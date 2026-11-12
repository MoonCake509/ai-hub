"""CTX-01: неизменяемые пакеты входных артефактов проекта.

Пакет — это зафиксированный набор файлов с версиями и хешами, манифестом,
бюджетом и объяснением, почему каждый файл попал или не попал внутрь.
Собирается из фактического состояния рабочего каталога на хосте (операция
list_files), а не из предположений хаба.

Инварианты:
  * секретные пути и чужие каталоги не попадают в пакет никогда (политика —
    единый модуль security_policy, тот же источник у host worker);
  * digest включает SHA-256 каждого фактически передаваемого файла и версию
    формата: изменение содержимого без смены размера меняет digest;
  * каждая ревизия неизменяема: смена содержимого после preview даёт новую
    ревизию со статусом conflict, история ревизий хранится отдельно;
  * materialized ставится только после сверки manifest и content hash каждого
    выбранного файла конкретной ревизии — не раньше.
"""
from __future__ import annotations

import fnmatch
import base64
import threading
import hashlib
import json
import os
import uuid

from . import context, db, security_policy

LIST_PAGE = 200

STATES = ("probing", "sealed", "materializing", "materialized", "conflict", "failed")

# Версия формата digest пакета. Смена контракта инвалидирует старые digest.
PACKET_FORMAT_VERSION = 2

# Пути, которые не покидают хост ни при каких условиях — единая политика
# с host worker (agents/laptop/workspace_host.py), см. security_policy.
SECRET_PATTERNS = security_policy.SECRET_PATTERNS
SKIP_DIRS = security_policy.DENY_DIRS


class PacketError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.details}


def normalize_path(path: str) -> str:
    """Приводит путь к виду проекта: относительный, без '..', без ведущих слэшей."""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        raise PacketError("empty_path", "пустой путь")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise PacketError("absolute_path", f"абсолютный путь запрещён: {raw}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PacketError("path_escape", f"выход за пределы проекта запрещён: {raw}")
    return "/".join(parts)


def is_secret(rel: str) -> bool:
    return security_policy.is_secret(rel)


def in_skipped_dir(rel: str) -> bool:
    return security_policy.in_skipped_dir(rel)


def matches(rel: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, pattern.rstrip("/") + "/*"):
            return pattern
    return None


def canonical(items: list[dict]) -> str:
    """Канонический вид манифеста: только выбранные файлы, отсортированные по пути."""
    lines = []
    for item in sorted((i for i in items if i["selected"]), key=lambda x: x["path"]):
        lines.append(f"{item['path']}|{int(item['size'])}|{item['sha256'] or ''}")
    return "\n".join(lines)


def evidence_canonical(evidence: list[dict]) -> str:
    """Канонический вид семантического контекста.

    В канон входит всё, что попадает кодеру и влияет на смысл: sha, диапазон, вид,
    blob, хеш текста, модель и score. Текст не входит — его удостоверяет text_sha256.
    """
    lines = []
    for item in sorted(evidence or [], key=lambda x: (str(x.get("path")), int(x.get("start_line") or 0))):
        lines.append(f"{item.get('sha')}|{item.get('path')}|{int(item.get('start_line') or 0)}"
                     f"|{int(item.get('end_line') or 0)}|{item.get('kind')}|{item.get('blob_oid')}"
                     f"|{item.get('text_sha256')}|{item.get('model')}|{round(float(item.get('score') or 0), 4)}")
    return "\n".join(lines)


def evidence_digest(base_digest: str, evidence: list[dict]) -> str:
    """Единственная формула digest пакета с evidence — общая для seal и _store_built."""
    body = str(base_digest)
    canonical_evidence = evidence_canonical(evidence)
    if canonical_evidence:
        body += "\n--evidence--\n" + canonical_evidence
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def digest(items: list[dict], version: int = PACKET_FORMAT_VERSION,
           evidence: list[dict] | None = None) -> str:
    """Digest пакета: SHA выбранных файлов + evidence + версия формата."""
    body = f"aihub-packet-v{version}\n" + canonical(items)
    return evidence_digest(body, evidence or [])


def select(entries: list[dict], requested: list[str], budget_bytes: int) -> tuple[list[dict], int, dict]:
    """Отбирает файлы под бюджет с объяснением по каждому.

    entries — фактический список от хоста: [{"path","size","sha256"}, ...].
    requested — шаблоны путей; пустой список означает «все файлы проекта».
    """
    if budget_bytes <= 0:
        raise PacketError("bad_budget", "бюджет должен быть положительным")
    wanted = [normalize_path(p) for p in (requested or [])]

    items: list[dict] = []
    used = 0
    stats = {"candidates": 0, "secret": 0, "skipped_dir": 0, "unmatched": 0, "over_budget": 0, "selected": 0}

    for entry in sorted(entries, key=lambda e: str(e.get("path") or "")):
        rel = normalize_path(entry.get("path"))
        size = int(entry.get("size") or 0)
        sha = str(entry.get("sha256") or "") or None
        stats["candidates"] += 1

        if is_secret(rel):
            stats["secret"] += 1
            items.append({"path": rel, "size": size, "sha256": sha, "selected": False,
                          "reason": "исключён: секретный путь"})
            continue
        if in_skipped_dir(rel):
            stats["skipped_dir"] += 1
            items.append({"path": rel, "size": size, "sha256": sha, "selected": False,
                          "reason": "исключён: служебный каталог"})
            continue
        pattern = matches(rel, wanted) if wanted else "__all__"
        if not pattern:
            stats["unmatched"] += 1
            items.append({"path": rel, "size": size, "sha256": sha, "selected": False,
                          "reason": "не запрошен"})
            continue
        if used + size > budget_bytes:
            stats["over_budget"] += 1
            items.append({"path": rel, "size": size, "sha256": sha, "selected": False,
                          "reason": f"не влезло в бюджет {budget_bytes} байт"})
            continue
        used += size
        stats["selected"] += 1
        reason = "все файлы проекта" if pattern == "__all__" else f"совпал с шаблоном {pattern}"
        items.append({"path": rel, "size": size, "sha256": sha, "selected": True, "reason": reason})

    return items, used, stats


# ---------- хранение ----------

def _packet_row(packet_id: str) -> dict | None:
    with db._lock:
        conn = db.connect()
        row = db._one(conn, db._qualify("select * from aihub.context_packets where id = :i"), i=packet_id)
        if not row:
            return None
        items = db._rows(conn, db._qualify(
            "select path, size, sha256, selected, reason from aihub.context_packet_items"
            " where packet_id = :i order by path"), i=packet_id)
    result = dict(row)
    result["items"] = [dict(x) for x in items]
    result["digest"] = result.get("digest")
    for key in ("created_at", "updated_at", "sealed_at", "materialized_at"):
        if result.get(key) is not None:
            result[key] = result[key].isoformat()
    return result


def get(packet_id: str) -> dict | None:
    return _packet_row(packet_id)


def list_for_project(project_id: str, limit: int = 50) -> list[dict]:
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "select id, project_id, conversation_id, state, revision, digest, budget_bytes, used_bytes,"
            " created_at, sealed_at from aihub.context_packets where project_id = :p"
            " order by created_at desc limit :l"), p=project_id, l=int(limit))
    out = []
    for row in rows:
        item = dict(row)
        for key in ("created_at", "sealed_at"):
            if item.get(key) is not None:
                item[key] = item[key].isoformat()
        out.append(item)
    return out


def create(project_id: str, requested: list[str], budget_bytes: int,
           conversation_id: str | None = None) -> dict:
    """Создаёт пакет в состоянии probing: наполнение придёт результатом задания хоста."""
    if budget_bytes <= 0:
        raise PacketError("bad_budget", "бюджет должен быть положительным")
    wanted = [normalize_path(p) for p in (requested or [])]
    packet_id = uuid.uuid4().hex
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "insert into aihub.context_packets(id, project_id, conversation_id, state, revision,"
            " budget_bytes, request) values (:i, :p, :c, 'probing', 1, :b, :r::jsonb) returning id"),
            i=packet_id, p=project_id, c=conversation_id, b=int(budget_bytes),
            r=json.dumps({"requested": wanted}, ensure_ascii=False))
    return {"packet_id": packet_id, "state": "probing", "project_id": project_id,
            "budget_bytes": int(budget_bytes), "requested": wanted}


def attach_job(packet_id: str, job_id: str, op: str) -> None:
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "update aihub.context_packets set provider_job_id = :j, provider_op = :o, updated_at = now()"
            " where id = :i returning id"), j=job_id, o=op, i=packet_id)


def _write_items(conn, packet_id: str, items: list[dict]) -> None:
    conn.run(db._qualify("delete from aihub.context_packet_items where packet_id = :i"), i=packet_id)
    for item in items:
        conn.run(db._qualify(
            "insert into aihub.context_packet_items(packet_id, path, size, sha256, selected, reason)"
            " values (:p, :path, :s, :h, :sel, :r)"
            " on conflict (packet_id, path) do update set size = excluded.size,"
            " sha256 = excluded.sha256, selected = excluded.selected, reason = excluded.reason"),
            p=packet_id, path=item["path"], s=int(item["size"]), h=item.get("sha256"),
            sel=bool(item["selected"]), r=item["reason"])


def _save_revision(conn, packet_id: str, revision: int, digest_value: str,
                   items: list[dict]) -> None:
    """Фиксирует ревизию в истории. Ревизии неизменяемы: on conflict не трогаем."""
    conn.run(db._qualify(
        "insert into aihub.context_packet_revisions(packet_id, revision, digest, items)"
        " values (:p, :r, :d, :i::jsonb) on conflict (packet_id, revision) do nothing"),
        p=packet_id, r=int(revision), d=digest_value,
        i=json.dumps(items, ensure_ascii=False))


def revision_items(packet_id: str, revision: int) -> list[dict] | None:
    """Манифест конкретной ревизии из истории — неизменяемый снимок пакета."""
    with db._lock:
        conn = db.connect()
        row = db._one(conn, db._qualify(
            "select items from aihub.context_packet_revisions"
            " where packet_id = :p and revision = :r"), p=packet_id, r=int(revision))
    if not row:
        return None
    items = row.get("items")
    if isinstance(items, str):
        items = json.loads(items or "[]")
    return items


def _write_evidence(conn, packet_id: str, revision: int, evidence: list[dict]) -> None:
    conn.run(db._qualify("delete from aihub.context_packet_evidence where packet_id = :i and revision = :r"),
             i=packet_id, r=int(revision))
    for position, item in enumerate(evidence or []):
        conn.run(db._qualify(
            "insert into aihub.context_packet_evidence(packet_id, revision, position, sha, path,"
            " start_line, end_line, kind, text, blob_oid, text_sha256, score, model)"
            " values (:p, :r, :pos, :sha, :path, :a, :b, :k, :t, :oid, :h, :score, :m)"),
            p=packet_id, r=int(revision), pos=position, sha=str(item.get("sha") or "")[:64],
            path=str(item.get("path") or "")[:400], a=int(item.get("start_line") or 1),
            b=int(item.get("end_line") or 1), k=str(item.get("kind") or "code")[:12],
            t=str(item.get("text") or "")[:20000], oid=str(item.get("blob_oid") or "")[:64],
            h=str(item.get("text_sha256") or "")[:64], score=float(item.get("score") or 0),
            m=str(item.get("model") or "")[:200])


def evidence(packet_id: str, revision: int) -> list[dict]:
    """Проверенные фрагменты ревизии — только они попадают в промпт кодера."""
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "select sha, path, start_line, end_line, kind, text, blob_oid, text_sha256, score, model"
            " from aihub.context_packet_evidence where packet_id = :p and revision = :r"
            " order by position"), p=packet_id, r=int(revision))
    return [dict(row) for row in rows]


def seal(packet_id: str, entries: list[dict], recheck: bool = False,
         evidence_: list[dict] | None = None, semantic_state: str = "") -> dict:
    """Фиксирует пакет по фактическому списку файлов хоста.

    При повторной сверке сравнивает хеши с прежней ревизией: совпало — пакет
    остаётся прежним, изменилось — новая ревизия со статусом conflict.
    """
    row = _packet_row(packet_id)
    if not row:
        raise PacketError("not_found", "пакет не найден")
    request = row.get("request") or {}
    if isinstance(request, str):
        request = json.loads(request)
    items, used, stats = select(entries, request.get("requested") or [], int(row["budget_bytes"]))
    evidence = evidence_ if evidence_ is not None else []
    semantic = {"state": semantic_state or ("collected" if evidence else "empty")}
    if evidence_ is None and request.get("sha") and (request.get("kind") == "context"
                                                     or request.get("requested") is not None):
        from . import semantic as semantic_module
        project = db.get_project(row.get("project_id") or "") or {}
        try:
            evidence, semantic = semantic_module.collect_evidence(
                project, str(request.get("sha") or ""), str(request.get("goal") or ""),
                int(request.get("budget_tokens") or 2000))
        except Exception as exc:  # noqa: BLE001 — контекст не блокирует приёмку
            evidence, semantic = [], {"state": "failed", "reason": str(exc)[:300]}
    new_digest = digest(items, evidence=evidence)

    if recheck and row.get("digest"):
        if new_digest == row["digest"]:
            with db._lock:
                conn = db.connect()
                db._one(conn, db._qualify(
                    "update aihub.context_packets set state = 'sealed', updated_at = now()"
                    " where id = :i returning id"), i=packet_id)
            return {"packet_id": packet_id, "state": "sealed", "digest": new_digest,
                    "changed": False, "revision": int(row["revision"])}
        with db._lock:
            conn = db.connect()
            _write_items(conn, packet_id, items)
            db._one(conn, db._qualify(
                "update aihub.context_packets set state = 'conflict', revision = revision + 1,"
                " digest = :d, used_bytes = :u, manifest = :m::jsonb, sealed_at = now(),"
                " updated_at = now() where id = :i returning revision"),
                d=new_digest, u=int(used), i=packet_id,
                m=json.dumps({"stats": stats, "previous_digest": row["digest"],
                              "semantic": semantic}, ensure_ascii=False))
            _write_evidence(conn, packet_id, int(row["revision"]) + 1, evidence)
            fresh = _packet_row(packet_id)
            _save_revision(conn, packet_id, int(fresh["revision"]), new_digest, items)
        return {"packet_id": packet_id, "state": "conflict", "digest": new_digest,
                "changed": True, "revision": int(fresh["revision"]),
                "previous_digest": row["digest"]}

    with db._lock:
        conn = db.connect()
        _write_items(conn, packet_id, items)
        db._one(conn, db._qualify(
            "update aihub.context_packets set state = 'sealed', digest = :d, used_bytes = :u,"
            " manifest = :m::jsonb, sealed_at = now(), updated_at = now()"
            " where id = :i returning id"),
            d=new_digest, u=int(used), i=packet_id,
            m=json.dumps({"stats": stats, "format_version": PACKET_FORMAT_VERSION,
                          "semantic": semantic}, ensure_ascii=False))
        _save_revision(conn, packet_id, int(row["revision"]), new_digest, items)
        _write_evidence(conn, packet_id, int(row["revision"]), evidence)
    return {"packet_id": packet_id, "state": "sealed", "digest": new_digest,
            "changed": not recheck, "revision": int(row["revision"]), "used_bytes": int(used),
            "stats": stats, "semantic": semantic, "evidence": len(evidence)}


def fail(packet_id: str, reason: str) -> None:
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "update aihub.context_packets set state = 'failed', manifest = :m::jsonb,"
            " updated_at = now() where id = :i returning id"),
            m=json.dumps({"error": reason}, ensure_ascii=False), i=packet_id)


def selected_paths(packet: dict) -> list[str]:
    return [item["path"] for item in packet.get("items") or [] if item.get("selected")]


def verify_materialized(packet: dict, digest_value: str | None) -> bool:
    return bool(digest_value) and digest_value == packet.get("digest")


def mark_materialized(packet_id: str, path: str, digest_value: str | None) -> None:
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "update aihub.context_packets set state = 'materialized', materialized_path = :p,"
            " materialized_digest = :d, materialized_at = now(), updated_at = now()"
            " where id = :i returning id"), p=path, d=digest_value, i=packet_id)


def file_digest(items: list[dict]) -> str:
    """Хеш отдельного файла для сверки — используется в тестах и предпросмотре."""
    return hashlib.sha256(json.dumps(items, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def safe_join(root: str, rel: str) -> str:
    """Склеивает путь и проверяет, что результат остался внутри корня."""
    base = os.path.abspath(root)
    full = os.path.abspath(os.path.join(base, *rel.split("/")))
    if os.path.commonpath([base, full]) != base:
        raise PacketError("path_escape", f"выход за пределы проекта: {rel}")
    return full

# ---------- жизненный цикл: задание на хосте -> пакет ----------

TERMINAL = {"succeeded", "failed", "cancelled", "timeout", "unknown"}


def create_context(project: dict, goal: str, entry: str, contracts: list[str],
                   budget_tokens: int, conversation_id: str | None = None,
                   sha: str = "") -> dict:
    """Запрос контекста под задачу: список файлов, затем содержимое входа и контрактов."""
    from . import workspace
    if not entry:
        raise PacketError("no_entry", "нужна точка входа (entry)")
    if budget_tokens <= 0:
        raise PacketError("bad_budget", "бюджет должен быть положительным")
    packet_id = uuid.uuid4().hex
    request = {"kind": "context", "goal": goal or "", "entry": normalize_path(entry),
               "contracts": [normalize_path(c) for c in contracts or []],
               "budget_tokens": int(budget_tokens), "requested": [],
               "sha": str(sha or "")}
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "insert into aihub.context_packets(id, project_id, conversation_id, state, revision,"
            " budget_bytes, request) values (:i, :p, :c, 'probing', 1, 0, :r::jsonb) returning id"),
            i=packet_id, p=project["id"], c=conversation_id,
            r=json.dumps(request, ensure_ascii=False))
    job = workspace.create_op_job(
        "list_files", {"root": project.get("workdir") or "", "glob": "**/*",
                       "limit": LIST_PAGE, "offset": 0},
        project.get("host_id") or "", project["id"])
    attach_job(packet_id, job["job"]["id"], "list_files")
    return {"packet_id": packet_id, "state": "probing", "kind": "context",
            "entry": request["entry"], "budget_tokens": int(budget_tokens)}


def start(project: dict, requested: list[str], budget_bytes: int,
          conversation_id: str | None = None, sha: str = "", goal: str = "") -> dict:
    """Создаёт пакет и ставит задание list_files на хост проекта."""
    from . import workspace
    packet = create(project["id"], requested, budget_bytes, conversation_id)
    if sha or goal:
        with db._lock:
            conn = db.connect()
            conn.run(db._qualify(
                "update aihub.context_packets set request = request || :extra::jsonb where id = :i"),
                extra=json.dumps({"sha": str(sha or ""), "goal": str(goal or "")}, ensure_ascii=False),
                i=packet["packet_id"])
    job = workspace.create_op_job(
        "list_files",
        {"root": project.get("workdir") or "", "glob": "**/*", "limit": LIST_PAGE, "offset": 0},
        project.get("host_id") or "", project["id"])
    attach_job(packet["packet_id"], job["job"]["id"], "list_files")
    packet.update({"job_id": job["job"]["id"], "root": job.get("root")})
    return packet


def _entries_from_listing(result: dict | None) -> list[dict]:
    items = ((result or {}).get("items") or [])
    out = []
    for item in items:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        out.append({"path": item["path"], "size": int(item.get("size") or 0),
                    "sha256": item.get("sha256")})
    return out


def _store_built(packet_id: str, built: dict, contents: dict[str, str] | None = None,
                 evidence: list[dict] | None = None,
                 semantic: dict | None = None) -> None:
    """Пишет собранный вход; изменившийся digest означает конфликт и новую ревизию.

    Семантический контекст входит в digest: смена набора фрагментов — это новая
    ревизия, даже если выбранные файлы не изменились.
    """
    evidence = evidence or []
    if evidence or "base_digest" not in built:
        built = dict(built)
        built["base_digest"] = str(built["digest"])
        built["digest"] = evidence_digest(built["base_digest"], evidence)
    if semantic:
        built = dict(built)
        built["semantic"] = semantic
    previous = (_packet_row(packet_id) or {}).get("digest")
    state = "conflict" if previous and previous != built["digest"] else "sealed"
    bump = ", revision = revision + 1" if state == "conflict" else ""
    payload = dict(built)
    if state == "conflict":
        payload["previous_digest"] = previous
    with db._lock:
        conn = db.connect()
        _write_items(conn, packet_id, built["items"])
        row = db._one(conn, db._qualify(
            "update aihub.context_packets set state = :st, digest = :d, used_bytes = :u,"
            " budget_bytes = :b, manifest = :m::jsonb, sealed_at = now(), updated_at = now()"
            + bump + " where id = :i returning revision"),
            st=state, d=built["digest"], u=int(built["used_tokens"]),
            b=int(built["budget_tokens"]), i=packet_id,
            m=json.dumps(payload, ensure_ascii=False))
        revision = int(row["revision"])
        conn.run(db._qualify(
            "delete from aihub.context_packet_files where packet_id = :p and revision = :r"),
            p=packet_id, r=revision)
        selected = {item["path"] for item in built["items"] if item.get("selected")}
        for path in sorted(selected):
            text = (contents or {}).get(path)
            item = next(item for item in built["items"] if item["path"] == path)
            if text is None:
                raise PacketError("context_missing", f"нет проверенного содержимого: {path}")
            raw = text.encode("utf-8")
            if len(raw) != int(item["size"]) or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise PacketError("context_hash_mismatch", f"содержимое не прошло сверку: {path}")
            conn.run(db._qualify(
                "insert into aihub.context_packet_files(packet_id, revision, path, sha256, size, content)"
                " values (:p, :r, :path, :sha, :size, :content)"),
                p=packet_id, r=revision, path=path, sha=item["sha256"],
                size=int(item["size"]), content=text)
        _save_revision(conn, packet_id, revision, built["digest"],
                       built["items"])
        _write_evidence(conn, packet_id, revision, evidence)


def verified_contents(packet_id: str, revision: int) -> list[dict]:
    """Returns only the selected UTF-8 bytes verified for an immutable revision."""
    with db._lock:
        conn = db.connect()
        return [dict(row) for row in db._rows(conn, db._qualify(
            "select path, sha256, size, content from aihub.context_packet_files"
            " where packet_id = :p and revision = :r order by path"),
            p=packet_id, r=int(revision))]


def _store_collected(packet_id: str, collected: list[dict]) -> None:
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "update aihub.context_packets set manifest = :m::jsonb, updated_at = now()"
            " where id = :i returning id"),
            m=json.dumps({"collected": collected}, ensure_ascii=False), i=packet_id)


_POLL_LOCK = threading.Lock()


def poll_active(limit: int = 20) -> int:
    """Продвигает пакеты, ожидающие результата задания хоста. Вызывается sweeper'ом.

    Тик не должен накладываться сам на себя: два прохода по одному пакету
    пишут items параллельно и роняют вставку дублем ключа.
    """
    from . import workspace
    if not _POLL_LOCK.acquire(blocking=False):
        return 0
    try:
        return _poll_active_locked(limit)
    finally:
        _POLL_LOCK.release()


def _poll_active_locked(limit: int = 20) -> int:
    from . import workspace
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "select id, state, provider_job_id, provider_op from aihub.context_packets"
            " where state in ('probing', 'materializing') and provider_job_id is not null"
            " order by updated_at limit :l"), l=int(limit))
    moved = 0
    for row in rows:
        try:
            job = workspace.job_result(row["provider_job_id"])
        except Exception:
            continue
        if not job or job.get("status") not in TERMINAL:
            continue
        moved += 1
        op = row.get("provider_op")
        if job["status"] != "succeeded":
            fail(row["id"], f"задание {op} завершилось статусом {job['status']}: {job.get('error') or ''}"[:400])
            continue
        if op in ("list_files", "recheck"):
            result = job.get("result") or {}
            entries = _entries_from_listing(result)
            if not entries:
                fail(row["id"], "хост не вернул ни одного файла")
                continue
            packet_row = _packet_row(row["id"]) or {}
            collected = (packet_row.get("manifest") or {}).get("collected") or []
            collected.extend(entries)
            next_offset = result.get("next_offset")
            if next_offset:
                _store_collected(row["id"], collected)
                project = db.get_project(packet_row.get("project_id") or "")
                if not project:
                    fail(row["id"], "проект пакета не найден")
                    continue
                next_job = workspace.create_op_job(
                    "list_files",
                    {"root": project.get("workdir") or "", "glob": "**/*",
                     "limit": LIST_PAGE, "offset": int(next_offset)},
                    project.get("host_id") or "", project["id"])
                attach_job(row["id"], next_job["job"]["id"], op)
                continue
            request = packet_row.get("request") or {}
            if isinstance(request, str):
                request = json.loads(request)
            if request.get("kind") == "context":
                wanted = [request.get("entry")] + list(request.get("contracts") or [])
                have = {e["path"] for e in collected}
                files = [{"path": path} for path in wanted if path and path in have]
                if not files:
                    fail(row["id"], "точка входа или контракты не найдены в проекте")
                    continue
                _store_collected(row["id"], collected)
                project = db.get_project(packet_row.get("project_id") or "")
                if not project:
                    fail(row["id"], "проект не найден")
                    continue
                read_job = workspace.create_op_job(
                    "read_files", {"root": project.get("workdir") or "", "files": files},
                    project.get("host_id") or "", project["id"])
                attach_job(row["id"], read_job["job"]["id"], "read")
                continue
            seal(row["id"], collected, recheck=(op == "recheck"))
        elif op == "read":
            packet_row = _packet_row(row["id"]) or {}
            request = packet_row.get("request") or {}
            if isinstance(request, str):
                request = json.loads(request)
            entries = (packet_row.get("manifest") or {}).get("collected") or []
            returned = {f.get("path"): f for f in ((job.get("result") or {}).get("files") or [])
                        if f.get("path")}
            expected = {e["path"]: e for e in entries}
            contents = {}
            verified_entries = []
            for path, file in returned.items():
                listed = expected.get(path)
                text = file.get("text")
                if not listed or not isinstance(text, str):
                    continue
                raw = text.encode("utf-8")
                actual_sha = hashlib.sha256(raw).hexdigest()
                if (int(file.get("offset") or 0) != 0
                        or int(file.get("length") or len(raw)) != len(raw)
                        or len(raw) != int(listed.get("size") or 0)
                        or actual_sha != (listed.get("sha256") or "")
                        or file.get("sha256") != listed.get("sha256")):
                    continue
                contents[path] = text
                verified_entries.append(listed)
            try:
                built = context.build(verified_entries, contents, request.get("goal") or "",
                                      request.get("entry") or "",
                                      request.get("contracts") or [],
                                      int(request.get("budget_tokens") or 0))
            except context.ContextError as exc:
                fail(row["id"], exc.message)
                continue
            collected_evidence: list[dict] = []
            semantic_state: dict = {}
            if request.get("sha"):
                from . import semantic as semantic_module
                project = db.get_project(packet_row.get("project_id") or "") or {}
                try:
                    collected_evidence, semantic_state = semantic_module.collect_evidence(
                        project, str(request.get("sha") or ""),
                        str(request.get("goal") or ""),
                        int(request.get("budget_tokens") or 2000))
                except Exception as exc:  # noqa: BLE001 — поиск не блокирует сборку
                    semantic_state = {"state": "failed", "reason": str(exc)[:300]}
            _store_built(row["id"], built, contents, evidence=collected_evidence,
                         semantic=semantic_state)
        elif op == "materialize":
            # Материализация выполняется синхронно в _materialize_sync: тик не
            # должен принимать результат порции параллельно с её сверкой.
            pass
        elif op == "snapshot":
            result = job.get("result") or {}
            mark_materialized(row["id"], result.get("snapshot_id") or "", result.get("tree_digest"))
    return moved


MATERIALIZE_CHUNK = 8


def _revision_manifest(packet: dict) -> tuple[int, list[dict]]:
    """Манифест конкретной ревизии: из истории, для старых пакетов — текущий."""
    revision = int(packet.get("revision") or 1)
    items = revision_items(packet["id"], revision)
    if items is None:
        items = packet.get("items") or []
    return revision, [dict(i) for i in items]


def start_materialize(packet_id: str) -> dict:
    """Материализация ТОЛЬКО выбранных файлов конкретной ревизии пакета.

    Весь проект не копируется: хаб запрашивает по read_files выбранные пути
    ревизии и сверяет sha256 каждого файла с манифестом до того, как поставить
    materialized. Подменённый ответ хоста (несовпадение hash) даёт conflict.
    """
    from . import workspace
    packet = _packet_row(packet_id)
    if not packet:
        raise PacketError("not_found", "пакет не найден")
    if packet["state"] not in ("sealed", "conflict"):
        raise PacketError("bad_state", f"пакет в состоянии {packet['state']}, нужен sealed")
    revision, items = _revision_manifest(packet)
    selected = [i for i in items if i.get("selected")]
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "delete from aihub.context_packet_files where packet_id = :p and revision = :r"),
            p=packet_id, r=revision)
    project = db.get_project(packet["project_id"])
    if not project:
        raise PacketError("no_project", "проект пакета не найден")
    for item in selected:
        if security_policy.is_secret(item["path"]):
            raise PacketError("denied_path", f"выбранный путь запрещён политикой: {item['path']}")
        if not item.get("sha256"):
            raise PacketError("no_hash", f"в манифесте ревизии нет sha256: {item['path']}")
    # Сверка порций ждёт заданий воркера, поэтому идёт в отдельном потоке:
    # синхронный вызов из обработчика заблокировал бы выдачу заданий.
    def _run() -> None:
        try:
            _materialize_sync(packet_id, project, revision, items, selected)
        except Exception as exc:  # noqa: BLE001 — пакет не должен зависнуть в materializing
            fail(packet_id, f"материализация прервана: {exc}")

    threading.Thread(target=_run, name=f"packet-{packet_id[:8]}", daemon=True).start()
    return {"packet_id": packet_id, "state": "materializing", "revision": revision,
            "files": len(selected)}


def _materialize_sync(packet_id: str, project: dict, revision: int,
                      items: list[dict], selected: list[dict]) -> dict:
    """Прочитать выбранные файлы порциями и сверить каждую с манифестом.

    Материализация идёт в одном месте: следующая порция запрашивается только
    после сверки предыдущей, поэтому поздний результат старого задания не может
    быть принят за новую порцию. Любое расхождение — conflict, а не «почти
    materialized».
    """
    import hashlib as _hashlib
    import time as _time
    from . import workspace

    by_path = {i["path"]: i for i in selected}
    pending = [i["path"] for i in selected]
    verified: list[str] = []
    contents: list[tuple[str, dict, str]] = []
    attempts = 0
    while pending:
        chunk = [by_path[p] for p in pending[:MATERIALIZE_CHUNK]]
        job = workspace.create_op_job(
            "read_files",
            {"root": project.get("workdir") or "",
             "files": [{"path": i["path"]} for i in chunk]},
            project.get("host_id") or "", project["id"])
        job_id = job["job"]["id"]
        with db._lock:
            conn = db.connect()
            db._one(conn, db._qualify(
                "update aihub.context_packets set state = 'materializing', provider_job_id = :j,"
                " provider_op = 'materialize', updated_at = now() where id = :i returning id"),
                j=job_id, i=packet_id)
        final = None
        deadline = _time.monotonic() + 300
        while _time.monotonic() < deadline:
            final = workspace.job_result(job_id) or {}
            if final.get("status") in ("succeeded", "failed", "cancelled"):
                break
            _time.sleep(0.4)
        attempts += 1
        returned = {e.get("path"): e for e in ((final or {}).get("result") or {}).get("files") or []}
        reason = None
        if (final or {}).get("status") != "succeeded":
            reason = f"чтение снимка не выполнено: {(final or {}).get('error') or 'job failed'}"
        else:
            for item in chunk:
                entry = returned.get(item["path"])
                if entry is None:
                    reason = f"хост не вернул выбранный файл: {item['path']}"
                    break
                if entry.get("sha256") != item.get("sha256") or int(entry.get("size") or -1) != int(item.get("size") or -2):
                    reason = f"содержимое не прошло SHA-256: {item['path']}"
                    break
                try:
                    raw = (entry.get("text") if isinstance(entry.get("text"), str)
                           else base64.b64decode(entry.get("content_b64") or "", validate=True).decode("utf-8"))
                except (ValueError, UnicodeDecodeError, TypeError):
                    reason = f"содержимое не является UTF-8: {item['path']}"
                    break
                contents.append((item["path"], item, raw))
                pending.remove(item["path"])
                if item["path"] not in verified:
                    verified.append(item["path"])
        if reason is not None:
            with db._lock:
                conn = db.connect()
                db._one(conn, db._qualify(
                    "update aihub.context_packets set state = 'conflict',"
                    " manifest = manifest || :m::jsonb, updated_at = now() where id = :i returning id"),
                    m=json.dumps({"materialize": {"error": reason, "revision": revision,
                                                  "attempts": attempts}}, ensure_ascii=False),
                    i=packet_id)
            return {"packet_id": packet_id, "state": "conflict", "error": reason,
                    "revision": revision, "verified": len(verified)}
        progress = {"materialize": {"revision": revision, "total": len(selected),
                                    "verified": verified, "pending": pending}}
        with db._lock:
            conn = db.connect()
            db._one(conn, db._qualify(
                "update aihub.context_packets set manifest = manifest || :m::jsonb,"
                " updated_at = now() where id = :i returning id"),
                m=json.dumps(progress, ensure_ascii=False), i=packet_id)
    if contents:
        with db._lock:
            conn = db.connect()
            for path, item, text in contents:
                conn.run(db._qualify(
                    "insert into aihub.context_packet_files(packet_id, revision, path, sha256, size, content)"
                    " values (:p, :r, :path, :sha, :size, :content)"
                    " on conflict (packet_id, revision, path) do update set"
                    " sha256 = excluded.sha256, size = excluded.size, content = excluded.content"),
                    p=packet_id, r=revision, path=path, sha=item["sha256"],
                    size=int(item["size"]), content=text)
    rev_digest = digest([{"path": i["path"], "size": i["size"],
                          "sha256": i.get("sha256"), "selected": True}
                         for i in items if i.get("selected")])
    packet = _packet_row(packet_id) or {}
    if not verify_materialized({"digest": packet.get("digest")}, rev_digest):
        with db._lock:
            conn = db.connect()
            db._one(conn, db._qualify(
                "update aihub.context_packets set state = 'conflict',"
                " manifest = manifest || :m::jsonb, updated_at = now() where id = :i returning id"),
                m=json.dumps({"materialize": {"error": "digest ревизии не совпал с digest пакета",
                                              "revision": revision}}, ensure_ascii=False),
                i=packet_id)
        return {"packet_id": packet_id, "state": "conflict", "revision": revision}
    mark_materialized(packet_id, f"hub://packets/{packet_id}@{revision}", rev_digest)
    return {"packet_id": packet_id, "state": "materialized", "revision": revision,
            "files": len(selected), "verified": len(verified)}


def _continue_materialize(packet_id: str, job: dict) -> None:
    """Сверка прочитанных файлов с манифестом ревизии; при полном совпадении —
    materialized, иначе conflict с причиной. Невыбранные файлы не запрашиваются."""
    from . import workspace
    packet = _packet_row(packet_id) or {}
    # Результат устаревшей порции не должен сверяться с новой: поставленный
    # provider_job_id — единственный актуальный запрос ревизии.
    if job.get("id") and packet.get("provider_job_id") and job["id"] != packet["provider_job_id"]:
        return
    project = db.get_project(packet.get("project_id") or "")
    if not project:
        fail(packet_id, "проект пакета не найден")
        return
    revision, items = _revision_manifest(packet)
    by_path = {i["path"]: i for i in items if i.get("selected")}
    state = (packet.get("manifest") or {}).get("materialize") or {}
    verified = list(state.get("verified") or [])
    pending = list(state.get("pending") or [])
    reason = None
    chunk_contents = []
    for entry in ((job.get("result") or {}).get("files") or []):
        path = entry.get("path")
        want = by_path.get(path)
        # Чужой или невыбранный файл из ответа хоста не принимается.
        if want is None:
            reason = f"хост вернул невыбранный файл: {path}"
            break
        got_sha = entry.get("sha256")
        if not got_sha or got_sha != (want.get("sha256") or None):
            reason = f"hash mismatch для {path}: хост вернул {got_sha!r}"
            break
        if int(entry.get("size") or -1) != int(want.get("size") or -2):
            reason = f"размер не сошёлся для {path}"
            break
        try:
            if isinstance(entry.get("text"), str):
                raw = entry["text"].encode("utf-8")
                text = entry["text"]
            else:
                raw = base64.b64decode(entry.get("content_b64") or "", validate=True)
                text = raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            reason = f"содержимое не является UTF-8: {path}"
            break
        if (len(raw) != int(want.get("size") or 0)
                or hashlib.sha256(raw).hexdigest() != want.get("sha256")):
            reason = f"содержимое не прошло SHA-256: {path}"
            break
        chunk_contents.append((path, want, text))
        if path in pending:
            pending.remove(path)
        if path not in verified:
            verified.append(path)
    if reason is None:
        returned = {e.get("path") for e in ((job.get("result") or {}).get("files") or [])}
        # Сверяем только ту порцию, которую просили этим заданием: остальные
        # выбранные файлы уходят следующим заданием, а не считаются пропавшими.
        requested = list(state.get("requested") or pending)
        missing = [p for p in requested if p in by_path and p not in returned]
        if missing:
            reason = f"хост не вернул выбранные файлы: {', '.join(missing[:3])}"
    if reason is not None:
        with db._lock:
            conn = db.connect()
            conn.run(db._qualify(
                "delete from aihub.context_packet_files where packet_id = :p and revision = :r"),
                p=packet_id, r=revision)
            db._one(conn, db._qualify(
                "update aihub.context_packets set state = 'conflict',"
                " manifest = manifest || :m::jsonb, updated_at = now()"
                " where id = :i returning id"),
                m=json.dumps({"materialize": {"error": reason, "revision": revision}},
                             ensure_ascii=False), i=packet_id)
        return
    if chunk_contents:
        with db._lock:
            conn = db.connect()
            for path, item, text in chunk_contents:
                conn.run(db._qualify(
                    "insert into aihub.context_packet_files(packet_id, revision, path, sha256, size, content)"
                    " values (:p, :r, :path, :sha, :size, :content)"
                    " on conflict (packet_id, revision, path) do update set"
                    " sha256 = excluded.sha256, size = excluded.size, content = excluded.content"),
                    p=packet_id, r=revision, path=path, sha=item["sha256"],
                    size=int(item["size"]), content=text)
    if pending:
        # Следующая порция выбранных файлов этой же ревизии.
        progress = {"materialize": {"revision": revision, "total": state.get("total"),
                                    "verified": verified, "pending": pending}}
        with db._lock:
            conn = db.connect()
            db._one(conn, db._qualify(
                "update aihub.context_packets set manifest = manifest || :m::jsonb,"
                " updated_at = now() where id = :i returning id"),
                m=json.dumps(progress, ensure_ascii=False), i=packet_id)
        chunk = [by_path[p] for p in pending if p in by_path][:MATERIALIZE_CHUNK]
        progress["materialize"]["requested"] = [i["path"] for i in chunk]
        next_job = workspace.create_op_job(
            "read_files",
            {"root": project.get("workdir") or "",
             "files": [{"path": i["path"]} for i in chunk]},
            project.get("host_id") or "", project["id"])
        with db._lock:
            conn = db.connect()
            db._one(conn, db._qualify(
                "update aihub.context_packets set provider_job_id = :j, updated_at = now()"
                " where id = :i returning id"), j=next_job["job"]["id"], i=packet_id)
        return
    # Все выбранные файлы сверены. materialized только сейчас.
    rev_digest = digest([{"path": i["path"], "size": i["size"],
                          "sha256": i.get("sha256"), "selected": True}
                         for i in items if i.get("selected")])
    if not verify_materialized({"digest": packet.get("digest")}, rev_digest):
        with db._lock:
            conn = db.connect()
            db._one(conn, db._qualify(
                "update aihub.context_packets set state = 'conflict',"
                " manifest = manifest || :m::jsonb, updated_at = now()"
                " where id = :i returning id"),
                m=json.dumps({"materialize": {"error": "digest ревизии не совпал с digest пакета",
                                              "revision": revision}}, ensure_ascii=False),
                i=packet_id)
        return
    mark_materialized(packet_id, f"hub://packets/{packet_id}@{revision}", rev_digest)


def start_recheck(packet_id: str) -> dict:
    from . import workspace
    packet = _packet_row(packet_id)
    if not packet:
        raise PacketError("not_found", "пакет не найден")
    project = db.get_project(packet["project_id"])
    if not project:
        raise PacketError("no_project", "проект пакета не найден")
    job = workspace.create_op_job(
        "list_files", {"root": project.get("workdir") or "", "glob": "**/*",
                       "limit": LIST_PAGE, "offset": 0},
        project.get("host_id") or "", project["id"])
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "update aihub.context_packets set state = 'probing', provider_job_id = :j,"
            " provider_op = 'recheck', updated_at = now() where id = :i returning id"),
            j=job["job"]["id"], i=packet_id)
    return {"packet_id": packet_id, "state": "probing", "job_id": job["job"]["id"]}


RETRY_STATES = {"no_index", "embed_failed", "verify_failed"}


def retry_semantic(limit: int = 5, min_age_s: int = 10) -> int:
    """Повторная сборка семантического контекста для уже запечатанных пакетов.

    Код не выдаётся, пока состояние retryable; этот проход даёт ему шанс дойти до
    collected без ручного вмешательства. Ревизия при этом растёт, история цела.
    """
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "select id, revision, manifest, request from aihub.context_packets"
            " where state = 'sealed' and manifest->'semantic'->>'state' = any(:states)"
            " and updated_at < now() - make_interval(secs => :age) order by updated_at limit :l"),
            states=list(RETRY_STATES), age=int(min_age_s), l=int(limit))
    retried = 0
    for row in rows:
        manifest = row.get("manifest") or {}
        request = row.get("request") or {}
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        if isinstance(request, str):
            request = json.loads(request)
        base = manifest.get("base_digest")
        if not base or not request.get("sha"):
            continue
        project = db.get_project(request.get("project_id") or "") or db.get_project(
            (_packet_row(row["id"]) or {}).get("project_id") or "")
        if not project:
            continue
        revision = int(row.get("revision") or 1)
        contents = {item["path"]: item["content"]
                    for item in verified_contents(row["id"], revision)}
        built = {key: value for key, value in manifest.items()
                 if key not in ("semantic", "base_digest", "previous_digest")}
        built["digest"] = base
        from . import semantic as semantic_module
        try:
            evidence, state = semantic_module.collect_evidence(
                project, str(request.get("sha") or ""), str(request.get("goal") or ""),
                int(request.get("budget_tokens") or 2000))
        except Exception as exc:  # noqa: BLE001
            evidence, state = [], {"state": "verify_failed", "reason": str(exc)[:300]}
        try:
            _store_built(row["id"], built, contents, evidence=evidence, semantic=state)
            retried += 1
        except Exception as exc:  # noqa: BLE001 — пакет не должен зависнуть
            fail(row["id"], f"повторная сборка контекста: {exc}"[:300])
    return retried
