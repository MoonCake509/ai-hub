"""Семантический поиск по коду и документации (P-09).

Векторы считают локальные модели на хосте воркера, хаб их только хранит и
сравнивает: файлов проекта контейнер не видит. Индекс привязан к коммиту —
поиск по чужому SHA не подмешивает свежие фрагменты.
"""
from __future__ import annotations

import hashlib
import json
import math
import time

from . import db


def store(project_id: str, sha: str, items: list[dict], model: str = "") -> dict:
    """Заменить индекс проекта для коммита. Повторный вызов идемпотентен."""
    if not isinstance(items, list):
        raise ValueError("items must be a list")
    stored = 0
    with db._lock:
        conn = db.connect()
        with db.transaction(conn):
            conn.run(db._qualify(
                "delete from aihub.semantic_chunks where project_id = :p and sha = :s"),
                p=project_id, s=sha)
            for item in items:
                vec = item.get("vector")
                if not isinstance(vec, list) or not vec:
                    continue
                db._one(conn, db._qualify(
                    "insert into aihub.semantic_chunks(project_id, sha, path, start_line,"
                    " end_line, kind, text, vector, model) values (:p, :s, :path, :a, :b,"
                    " :k, :t, :v::jsonb, :m) returning id"),
                    p=project_id, s=sha, path=str(item.get("path") or "")[:400],
                    a=int(item.get("start") or 1), b=int(item.get("end") or 1),
                    k=str(item.get("kind") or "docs")[:12],
                    t=str(item.get("text") or "")[:8000],
                    v=json.dumps([round(float(x), 6) for x in vec]),
                    m=str(model or "")[:200])
                stored += 1
    return {"sha": sha, "stored": stored}


def kinds(project_id: str, sha: str = "") -> list[str]:
    """Какие виды фрагментов есть в индексе — под них и считаем векторы запроса."""
    conn = db.connect()
    sha = sha or (db._one(conn, db._qualify(
        "select sha from aihub.semantic_chunks where project_id = :p"
        " order by created_at desc limit 1"), p=project_id) or {}).get("sha") or ""
    if not sha:
        return []
    rows = db._rows(conn, db._qualify(
        "select distinct kind from aihub.semantic_chunks where project_id = :p and sha = :s"),
        p=project_id, s=sha)
    return [row["kind"] for row in rows if row.get("kind")]


def models(project_id: str, sha: str) -> dict[str, str]:
    """Модель, которой построен индекс каждого вида фрагментов.

    Запросный вектор обязан считаться той же моделью: смешивать пространства
    разных моделей нельзя, иначе score бессмыслен.
    """
    conn = db.connect()
    rows = db._rows(conn, db._qualify(
        "select kind, model from aihub.semantic_chunks where project_id = :p and sha = :s"
        " group by kind, model"), p=project_id, s=sha)
    out: dict[str, str] = {}
    for row in rows:
        out.setdefault(str(row.get("kind") or "docs"), str(row.get("model") or ""))
    return out


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else -1.0


def search(project_id: str, vectors: dict[str, list[float]], sha: str = "",
           limit: int = 10, models_map: dict[str, str] | None = None) -> dict:
    """Топ фрагментов по косинусу. Каждый вид фрагментов сравнивается со своим вектором."""
    conn = db.connect()
    if sha:
        rows = db._rows(conn, db._qualify(
            "select path, start_line, end_line, kind, text, vector, sha, model from aihub.semantic_chunks"
            " where project_id = :p and sha = :s"), p=project_id, s=sha)
    else:
        latest = db._one(conn, db._qualify(
            "select sha from aihub.semantic_chunks where project_id = :p"
            " order by created_at desc limit 1"), p=project_id)
        if not latest:
            return {"sha": "", "matches": [], "indexed": False}
        sha = latest["sha"]
        rows = db._rows(conn, db._qualify(
            "select path, start_line, end_line, kind, text, vector, sha, model from aihub.semantic_chunks"
            " where project_id = :p and sha = :s"), p=project_id, s=sha)
    scored = []
    for row in rows:
        vec = row.get("vector")
        if isinstance(vec, str):
            vec = json.loads(vec)
        if not isinstance(vec, list):
            continue
        query_vec = vectors.get(row.get("kind") or "docs")
        if not query_vec:
            continue
        if models_map is not None and str(row.get("model") or "") != models_map.get(str(row.get("kind") or "docs"), ""):
            continue
        score = _cosine(query_vec, [float(x) for x in vec])
        scored.append({"path": row["path"], "start_line": row["start_line"],
                       "end_line": row["end_line"], "kind": row["kind"],
                       "score": round(score, 4), "sha": row["sha"],
                       "text": (row.get("text") or "")[:1200]})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return {"sha": sha, "indexed": True, "matches": scored[:max(1, min(50, limit))]}


# ---------- проверяемый семантический контекст (P-09, вторая половина) ----------

EVIDENCE_LIMIT = 5
EVIDENCE_TOKENS = 1500


def _op(project: dict, op: str, args: dict, timeout_s: int = 90) -> dict:
    """Короткий вызов операции хоста с ожиданием: вектор и проверка blob'ов."""
    from . import workspace

    job = workspace.create_op_job(op, args, project.get("host_id") or "", project.get("id") or "")
    job_id = job["job"]["id"]
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        row = workspace.job_result(job_id)
        if row and row.get("status") in ("succeeded", "failed", "cancelled", "expired"):
            if row["status"] != "succeeded":
                raise RuntimeError(f"{op}: {str(row.get('error') or row['status'])[:200]}")
            return row.get("result") or {}
        time.sleep(0.4)
    raise TimeoutError(f"{op}: хост не ответил за {timeout_s}s")


def _overlaps(left: dict, right: dict) -> bool:
    """Любое пересечение диапазонов — дубль: соседние чанки перекрываются намеренно."""
    if left.get("path") != right.get("path"):
        return False
    a, b = int(left["start_line"]), int(left["end_line"])
    c, d = int(right["start_line"]), int(right["end_line"])
    return min(b, d) >= max(a, c)


def collect_evidence(project: dict, sha: str, goal: str, budget_tokens: int = 2000) -> tuple[list[dict], dict]:
    """Ищет фрагменты по цели и подтверждает их у хоста по blob'ам конкретного SHA.

    В промпт уходит только то, что хост прочитал из blob'а указанного коммита и
    заверил хешем текста. Пустой индекс, отсутствие модели и ошибка хоста не
    блокируют запуск — состояние пишется в манифест пакета.
    """
    if not sha or not goal.strip():
        return [], {"state": "skipped", "reason": "нет sha или цели"}
    project_id = project.get("id") or ""
    root = project.get("workdir") or ""
    kinds_here = kinds(project_id, sha)
    if not kinds_here:
        # Индекс нужного SHA строится с ограниченным ожиданием: без него код не выдаётся.
        try:
            _op(project, "semantic_index",
                {"root": root, "sha": sha, "project_id": project_id, "upload": True}, 300)
        except Exception as exc:  # noqa: BLE001
            return [], {"state": "no_index", "sha": sha, "reason": str(exc)[:300]}
        kinds_here = kinds(project_id, sha)
        if not kinds_here:
            return [], {"state": "no_index", "sha": sha, "reason": "индекс пуст после сборки"}
    try:
        models_map = models(project_id, sha)
    except Exception:  # noqa: BLE001 — в юнит-тестах БД нет; фильтр по модели тогда не применяется
        models_map = {}
    vectors: dict[str, list[float]] = {}
    try:
        for kind in kinds_here:
            embedded = _op(project, "semantic_embed",
                           {"root": root, "text": goal[:2000], "kind": kind}, 120)
            vector = embedded.get("vector")
            if isinstance(vector, list) and vector:
                vectors[kind] = [float(x) for x in vector]
    except Exception as exc:  # noqa: BLE001
        return [], {"state": "embed_failed", "sha": sha, "reason": str(exc)[:300]}
    if not vectors:
        return [], {"state": "no_vectors", "sha": sha}
    # Пустая карта моделей означает «фильтр неизвестен»: иначе сравнение с "" отбросит всё.
    found = search(project_id, vectors, sha, limit=12,
                   models_map=models_map or None)
    candidates: list[dict] = []
    for hit in found.get("matches") or []:
        if float(hit.get("score") or 0) <= 0:
            continue
        item = {"path": hit["path"], "start_line": int(hit["start_line"]),
                "end_line": int(hit["end_line"]), "kind": hit.get("kind") or "code",
                "score": float(hit.get("score") or 0)}
        if any(_overlaps(item, seen) for seen in candidates):
            continue
        candidates.append(item)
        if len(candidates) >= EVIDENCE_LIMIT * 2:
            break
    if not candidates:
        return [], {"state": "empty", "sha": sha}
    try:
        checked = _op(project, "verify_evidence",
                      {"root": root, "sha": sha, "items": candidates}, 120)
    except Exception as exc:  # noqa: BLE001
        return [], {"state": "verify_failed", "sha": sha, "reason": str(exc)[:300]}
    by_key = {(c["path"], c["start_line"], c["end_line"]): c["score"] for c in candidates}
    verified: list[dict] = []
    used_tokens = 0
    for item in checked.get("verified") or []:
        tokens = max(1, len(str(item.get("text") or "")) // 4)
        limit_tokens = max(0, min(EVIDENCE_TOKENS, int(budget_tokens)))
        if limit_tokens == 0 or used_tokens + tokens > limit_tokens:
            continue
        used_tokens += tokens
        item["score"] = by_key.get((item["path"], item["start_line"], item["end_line"]), 0.0)
        item["model"] = models_map.get(str(item.get("kind") or "code"), "")
        verified.append(item)
        if len(verified) >= EVIDENCE_LIMIT:
            break
    state = "collected" if verified else "rejected"
    return verified, {"state": state, "sha": sha, "kinds": kinds_here,
                      "query_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16],
                      "candidates": len(candidates), "verified": len(verified),
                      "dropped": len(checked.get("dropped") or []),
                      "tokens": used_tokens}
