"""CTX-01: неизменяемые пакеты входных артефактов проекта.

Пакет — это зафиксированный набор файлов с версиями и хешами, манифестом,
бюджетом и объяснением, почему каждый файл попал или не попал внутрь.
Собирается из фактического состояния рабочего каталога на хосте (операция
list_files), а не из предположений хаба.

Инварианты:
  * секретные пути и чужие каталоги не попадают в пакет никогда;
  * одинаковый набор файлов даёт одинаковый digest;
  * изменение файла после сборки обнаруживается сверкой и даёт конфликт,
    а не тихую подмену входа.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import uuid

from . import db

LIST_PAGE = 200

STATES = ("probing", "sealed", "materializing", "materialized", "conflict", "failed")

# Пути, которые не покидают хост ни при каких условиях.
SECRET_PATTERNS = (
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*",
    "*credentials*", "*secret*", ".npmrc", ".pypirc", ".netrc", ".git-credentials",
    "*.kdbx", "*.ovpn", "auth.json", "cookies*.txt",
)
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".next", ".idea", ".mypy_cache", ".pytest_cache", "target", "vendor"}


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
    name = rel.rsplit("/", 1)[-1]
    for pattern in SECRET_PATTERNS:
        if fnmatch.fnmatch(name.lower(), pattern.lower()):
            return True
        if "*" in pattern and fnmatch.fnmatch(rel.lower(), pattern.lower()):
            return True
    return False


def in_skipped_dir(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in rel.split("/")[:-1])


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


def digest(items: list[dict]) -> str:
    return hashlib.sha256(canonical(items).encode("utf-8")).hexdigest()


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
            " values (:p, :path, :s, :h, :sel, :r)"),
            p=packet_id, path=item["path"], s=int(item["size"]), h=item["sha256"],
            sel=bool(item["selected"]), r=item["reason"])


def seal(packet_id: str, entries: list[dict], recheck: bool = False) -> dict:
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
    new_digest = digest(items)

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
                m=json.dumps({"stats": stats, "previous_digest": row["digest"]}, ensure_ascii=False))
        fresh = _packet_row(packet_id)
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
            m=json.dumps({"stats": stats}, ensure_ascii=False))
    return {"packet_id": packet_id, "state": "sealed", "digest": new_digest,
            "changed": not recheck, "revision": int(row["revision"]), "used_bytes": int(used),
            "stats": stats}


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


def start(project: dict, requested: list[str], budget_bytes: int,
          conversation_id: str | None = None) -> dict:
    """Создаёт пакет и ставит задание list_files на хост проекта."""
    from . import workspace
    packet = create(project["id"], requested, budget_bytes, conversation_id)
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


def _store_collected(packet_id: str, collected: list[dict]) -> None:
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "update aihub.context_packets set manifest = :m::jsonb, updated_at = now()"
            " where id = :i returning id"),
            m=json.dumps({"collected": collected}, ensure_ascii=False), i=packet_id)


def poll_active(limit: int = 20) -> int:
    """Продвигает пакеты, ожидающие результата задания хоста. Вызывается sweeper'ом."""
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
            seal(row["id"], collected, recheck=(op == "recheck"))
        elif op == "snapshot":
            result = job.get("result") or {}
            mark_materialized(row["id"], result.get("snapshot_id") or "", result.get("tree_digest"))
    return moved


def start_materialize(packet_id: str) -> dict:
    """Материализация на хосте: управляемый снимок проекта под этот пакет."""
    from . import workspace
    packet = _packet_row(packet_id)
    if not packet:
        raise PacketError("not_found", "пакет не найден")
    if packet["state"] not in ("sealed", "conflict"):
        raise PacketError("bad_state", f"пакет в состоянии {packet['state']}, нужен sealed")
    project = db.get_project(packet["project_id"])
    if not project:
        raise PacketError("no_project", "проект пакета не найден")
    job = workspace.create_op_job(
        "snapshot", {"root": project.get("workdir") or "", "label": f"packet-{packet_id[:8]}"},
        project.get("host_id") or "", project["id"])
    with db._lock:
        conn = db.connect()
        db._one(conn, db._qualify(
            "update aihub.context_packets set state = 'materializing', provider_job_id = :j,"
            " provider_op = 'snapshot', updated_at = now() where id = :i returning id"),
            j=job["job"]["id"], i=packet_id)
    return {"packet_id": packet_id, "state": "materializing", "job_id": job["job"]["id"]}


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
