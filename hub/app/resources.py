"""CORE-02 — аренды ресурсов: один checkout/CDP/CLI/модель — один писатель.

Ключ строится из фактического адреса ресурса, а не из имени агента: один и тот
же CDP, доступный через два alias, остаётся одним ресурсом. Аренда выдаётся в
той же транзакции, что и аренда задания (`db.lease_job`), поэтому «двое пишут в
один checkout» невозможно даже при одновременном опросе двух воркеров.

Аренда имеет поколение (`generation`, он же fencing token) и срок. Освободить
чужую аренду нельзя: `release` фильтрует по владельцу-заданию. Истёкшая аренда
переиспользуется с новым поколением — поздний держатель старого поколения не
должен приниматься за актуального.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import db

KINDS = ("checkout", "cdp", "cli", "model")
DEFAULT_TTL_S = 900

DDL = [
    """create table if not exists aihub.resource_leases (
        key text primary key,
        kind text not null,
        host_id text not null default '',
        holder text,
        job_id text,
        generation int not null default 1,
        acquired_at timestamptz not null default now(),
        lease_until timestamptz not null,
        released_at timestamptz,
        meta jsonb not null default '{}'
    )""",
    "create index if not exists resource_leases_until on aihub.resource_leases(lease_until)",
    "create index if not exists resource_leases_job on aihub.resource_leases(job_id)",
]


def init() -> None:
    with db._lock:
        conn = db.connect()
        for stmt in DDL:
            conn.run(db._qualify(stmt))


def _norm_target(kind: str, target: str) -> str:
    """Приводит адрес ресурса к каноническому виду.

    Windows-путь: обратные слэши, нижний регистр, без хвостового слэша — иначе
    `R:/Proj`, `r:/proj/` и `R:/Proj/` оказались бы тремя разными checkout.
    CDP: `localhost` и `127.0.0.1` — один browser, порт обязателен, потому что
    один хост может держать несколько инстансов.
    """
    value = (target or "").strip()
    if not value:
        raise ValueError("resource target is required")
    if ".." in value.replace("\\", "/").split("/"):
        raise ValueError("resource target must not contain parent references")
    if kind == "checkout":
        return value.replace("/", "\\").rstrip("\\").lower()
    if kind == "cdp":
        host, sep, port = value.rpartition(":")
        if not sep or not port.strip().isdigit():
            raise ValueError("cdp target must be host:port")
        host = host.strip().lower()
        if host in ("localhost", "::1", "0.0.0.0"):
            host = "127.0.0.1"
        return host + ":" + str(int(port))
    if kind == "cli":
        return value.lower()
    return value.lower()


def key_for(kind: str, host_id: str, target: str) -> str:
    """Ключ ресурса: вид + устройство + канонический адрес."""
    if kind not in KINDS:
        raise ValueError("unsupported resource kind: " + str(kind))
    host = (host_id or "").strip().lower()
    if not host:
        raise ValueError("host_id is required for a resource key")
    return kind + ":" + host + ":" + _norm_target(kind, target)


def _iso(value) -> str | None:
    """Время в ответе API — строка: JSONResponse не умеет datetime."""
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def acquire(conn, key: str, holder: str, job_id: str, ttl_s: int = DEFAULT_TTL_S,
            meta: dict | None = None) -> dict | None:
    """Взять аренду в текущей транзакции. None — ресурс занят живой арендой.

    Свободна, если аренды нет, она истекла или принадлежит этому же заданию
    (повторный шаг того же run продолжает своё поколение).
    """
    import json

    kind, _, rest = key.partition(":")
    host_id = rest.partition(":")[0]
    row = db._one(
        conn,
        db._qualify(
            """insert into aihub.resource_leases(key, kind, host_id, holder, job_id,
                                                generation, acquired_at, lease_until, released_at, meta)
               values (:k, :kind, :host, :holder, :job, 1, now(),
                       now() + (:ttl || ' seconds')::interval, null, :meta::jsonb)
               on conflict (key) do update set
                    holder = excluded.holder,
                    job_id = excluded.job_id,
                    generation = aihub.resource_leases.generation + 1,
                    acquired_at = now(),
                    lease_until = excluded.lease_until,
                    released_at = null,
                    meta = excluded.meta
                where (aihub.resource_leases.lease_until < now()
                   or aihub.resource_leases.job_id = :job)
               and not exists (
                   select 1 from aihub.jobs live
                    where live.id = aihub.resource_leases.job_id
                      and live.status in ('running', 'unknown', 'cancel_requested')
               )
               returning key, generation, lease_until, holder, job_id"""),
        k=key, kind=kind, host=host_id, holder=holder, job=job_id,
        ttl=str(int(ttl_s)), meta=json.dumps(meta or {}, ensure_ascii=False),
    )
    if not row:
        return None
    return {"key": row["key"], "generation": int(row["generation"]),
            "holder": row["holder"], "job_id": row["job_id"],
            "lease_until": _iso(row["lease_until"])}


def holder_of(conn, key: str) -> dict | None:
    row = db._one(conn, db._qualify(
        "select key, kind, host_id, holder, job_id, generation, lease_until, released_at"
        " from aihub.resource_leases where key = :k and lease_until > now()"), k=key)
    return dict(row) if row else None


def renew_for_job(conn, job_id: str, generation: int | None, ttl_s: int) -> dict | None:
    """Renew a checkout only while this exact generation still owns an unexpired lease."""
    return db._one(conn, db._qualify(
        "update aihub.resource_leases set lease_until = now() + (:ttl || ' seconds')::interval"
        " where job_id = :job and (CAST(:generation AS integer) is null or generation = CAST(:generation AS integer))"
        " and lease_until > now()"
        " returning key, generation, lease_until, job_id"),
        ttl=str(int(ttl_s)), job=job_id,
        generation=int(generation) if generation is not None else None)


def release_for_job(conn, job_id: str) -> int:
    """Освободить аренды задания, сохранив строку и поколение.

    Удалять строку нельзя: следующая аренда получила бы поколение 1, и поздний
    держатель старого поколения снова выглядел бы актуальным. Поэтому аренда
    помечается освобождённой, а поколение остаётся монотонным.
    """
    row = db._one(conn, db._qualify(
        "with freed as (update aihub.resource_leases"
        " set released_at = now(), lease_until = now(), job_id = null, holder = null"
        " where job_id = :j returning key) select count(*) as n from freed"), j=job_id)
    return int(row["n"] if row else 0)


def release(key: str, job_id: str) -> bool:
    with db._lock:
        conn = db.connect()
        row = db._one(conn, db._qualify(
            "with freed as (update aihub.resource_leases"
            " set released_at = now(), lease_until = now(), job_id = null, holder = null"
            " where key = :k and job_id = :j returning key) select count(*) as n from freed"),
            k=key, j=job_id)
        return bool(row and int(row["n"]))


def sweep() -> int:
    """Освобождает истёкшие аренды: без этого ресурс оставался бы занятым навсегда."""
    with db._lock:
        conn = db.connect()
        row = db._one(conn, db._qualify(
            "with freed as (update aihub.resource_leases"
            " set released_at = coalesce(released_at, now()), job_id = null, holder = null"
            " where job_id is not null"
            # Держит ресурс только живая аренда. Задание, которое хаб потерял при
            # рестарте, уже не пишет в checkout: если оставить его блокировку,
            # проект навсегда перестаёт получать новые задания.
            " and not exists (select 1 from aihub.jobs j where j.id = aihub.resource_leases.job_id"
            " and j.status in ('running', 'cancel_requested') and j.lease_until > now()) returning key)"
            " select count(*) as n from freed"))
        return int(row["n"] if row else 0)


def list_leases() -> list[dict]:
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "select key, kind, host_id, holder, job_id, generation, acquired_at, lease_until, meta"
            " from aihub.resource_leases"
            " where job_id is not null and lease_until > now()"
            " order by acquired_at desc"))
    items = []
    for row in rows:
        item = dict(row)
        item["acquired_at"] = _iso(item.get("acquired_at"))
        item["lease_until"] = _iso(item.get("lease_until"))
        items.append(item)
    return items
