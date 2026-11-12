"""Семантический поиск по коду и документации (P-09).

Векторы считают локальные модели на хосте воркера, хаб их только хранит и
сравнивает: файлов проекта контейнер не видит. Индекс привязан к коммиту —
поиск по чужому SHA не подмешивает свежие фрагменты.
"""
from __future__ import annotations

import json
import math

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


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return -1.0
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else -1.0


def search(project_id: str, vectors: dict[str, list[float]], sha: str = "",
           limit: int = 10) -> dict:
    """Топ фрагментов по косинусу. Каждый вид фрагментов сравнивается со своим вектором."""
    conn = db.connect()
    if sha:
        rows = db._rows(conn, db._qualify(
            "select path, start_line, end_line, kind, text, vector, sha from aihub.semantic_chunks"
            " where project_id = :p and sha = :s"), p=project_id, s=sha)
    else:
        latest = db._one(conn, db._qualify(
            "select sha from aihub.semantic_chunks where project_id = :p"
            " order by created_at desc limit 1"), p=project_id)
        if not latest:
            return {"sha": "", "matches": [], "indexed": False}
        sha = latest["sha"]
        rows = db._rows(conn, db._qualify(
            "select path, start_line, end_line, kind, text, vector, sha from aihub.semantic_chunks"
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
        score = _cosine(query_vec, [float(x) for x in vec])
        scored.append({"path": row["path"], "start_line": row["start_line"],
                       "end_line": row["end_line"], "kind": row["kind"],
                       "score": round(score, 4), "sha": row["sha"],
                       "text": (row.get("text") or "")[:1200]})
    scored.sort(key=lambda item: item["score"], reverse=True)
    return {"sha": sha, "indexed": True, "matches": scored[:max(1, min(50, limit))]}
