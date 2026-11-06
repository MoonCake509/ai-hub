"""Postgres-слой AI-Hub: задания, попытки, события.

Схема в отдельной схеме `aihub` той же базы, что использует LiteLLM.
Клиент — pg8000 (чистый Python, без сборки бинарников).
Гарантии, заложенные осознанно:
  * задание фиксируется до ответа клиенту;
  * аренда попытки вместо автоматического перезапуска: истёкшая аренда
    помечает попытку `unknown`, а не запускает агента заново;
  * приём результата идемпотентен по attempt_id, устаревшая попытка не перезаписывает актуальную.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from urllib.parse import urlparse

import pg8000.native

_lock = threading.RLock()
_conn: pg8000.native.Connection | None = None

DDL = [
    "create schema if not exists aihub",
    """create table if not exists aihub.jobs (
        id text primary key,
        agent_id text not null,
        prompt text not null,
        prompt_sha text not null,
        timeout_s int not null,
        status text not null default 'queued',
        idempotency_key text,
        attempts int not null default 0,
        needs_reconcile boolean not null default false,
        created_at timestamptz not null default now(),
        started_at timestamptz,
        finished_at timestamptz,
        result_output text,
        result_error text,
        result_usage jsonb,
        lease_until timestamptz,
        worker text
    )""",
    """create unique index if not exists jobs_idem
        on aihub.jobs(idempotency_key) where idempotency_key is not null""",
    "create index if not exists jobs_status on aihub.jobs(status, created_at desc)",
    """create table if not exists aihub.job_attempts (
        id text primary key,
        job_id text not null references aihub.jobs(id) on delete cascade,
        attempt_no int not null,
        worker text,
        status text not null default 'running',
        lease_until timestamptz,
        started_at timestamptz not null default now(),
        finished_at timestamptz,
        output text,
        error text,
        usage jsonb
    )""",
    "create index if not exists attempts_job on aihub.job_attempts(job_id, attempt_no desc)",
    """create table if not exists aihub.late_results (
        id bigserial primary key,
        job_id text,
        attempt_id text not null,
        attempt_status text,
        payload jsonb not null,
        received_at timestamptz not null default now(),
        reviewed_at timestamptz,
        note text
    )""",
    "create unique index if not exists late_attempt on aihub.late_results(attempt_id)",
    """create table if not exists aihub.job_events (
        id bigserial primary key,
        job_id text not null references aihub.jobs(id) on delete cascade,
        attempt_id text,
        event text not null,
        at timestamptz not null default now(),
        payload jsonb
    )""",
]


def _dsn() -> dict:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    p = urlparse(url)
    return {
        "user": p.username or "llmproxy",
        "password": p.password or "",
        "host": p.hostname or "postgres",
        "port": int(p.port or 5432),
        "database": (p.path or "/postgres").lstrip("/"),
    }


def connect() -> pg8000.native.Connection:
    global _conn
    if _conn is None:
        _conn = pg8000.native.Connection(**_dsn(), timeout=30)
    return _conn


def init() -> None:
    with _lock:
        conn = connect()
        for stmt in DDL:
            conn.run(stmt)


def _rows(conn, sql: str, **params) -> list[dict]:
    result = conn.run(sql, **params)
    cols = [c["name"] for c in conn.columns] if conn.columns else []
    return [dict(zip(cols, row)) for row in result]


def _one(conn, sql: str, **params) -> dict | None:
    rows = _rows(conn, sql, **params)
    return rows[0] if rows else None


def event(job_id: str, name: str, attempt_id: str | None = None, payload: dict | None = None) -> None:
    with _lock:
        connect().run(
            "insert into aihub.job_events(job_id, attempt_id, event, payload)"
            " values (:j, :a, :e, :p::jsonb)",
            j=job_id, a=attempt_id, e=name, p=json.dumps(payload or {}, ensure_ascii=False),
        )


class QueueFull(Exception):
    """Очередь достигла предела: новое задание не принимаем."""


def create_job(agent_id: str, prompt: str, prompt_sha: str, timeout_s: int,
               idempotency_key: str | None, max_queue: int | None = None) -> tuple[dict, bool]:
    """Возвращает (job, created). При совпадении ключа с другим телом — ValueError."""
    job_id = uuid.uuid4().hex
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            # Проверка места в очереди и вставка должны быть одной операцией:
            # иначе два процесса увидят одинаково свободное место и превысят лимит.
            conn.run("select pg_advisory_xact_lock(7401)")
            if idempotency_key:
                existing = _one(conn, "select * from aihub.jobs where idempotency_key = :k", k=idempotency_key)
                if existing:
                    if (existing["prompt_sha"] != prompt_sha
                            or existing["agent_id"] != agent_id
                            or int(existing["timeout_s"]) != int(timeout_s)):
                        raise ValueError("idempotency_key_conflict")
                    conn.run("commit")
                    return existing, False
            if max_queue:
                depth = _one(conn, "select count(*) as n from aihub.jobs where status = 'queued'")["n"]
                if depth >= max_queue:
                    raise QueueFull(depth)
            conn.run(
                "insert into aihub.jobs(id, agent_id, prompt, prompt_sha, timeout_s, idempotency_key)"
                " values (:i, :a, :p, :s, :t, :k)",
                i=job_id, a=agent_id, p=prompt, s=prompt_sha, t=timeout_s, k=idempotency_key,
            )
            conn.run("insert into aihub.job_events(job_id, event) values (:j, 'queued')", j=job_id)
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return get_job(job_id), True


def get_job(job_id: str) -> dict | None:
    with _lock:
        conn = connect()
        job = _one(conn, "select * from aihub.jobs where id = :i", i=job_id)
        if not job:
            return None
        job["attempts_detail"] = _rows(
            conn,
            "select id, attempt_no, worker, status, started_at, finished_at, error, usage"
            " from aihub.job_attempts where job_id = :i order by attempt_no desc",
            i=job_id,
        )
        return job


def list_jobs(status: str | None, limit: int = 50, offset: int = 0) -> list[dict]:
    with _lock:
        conn = connect()
        if status:
            return _rows(conn, "select id, agent_id, status, created_at, finished_at, attempts,"
                               " left(prompt, 160) as prompt_preview"
                               " from aihub.jobs where status = :s order by created_at desc"
                               " limit :l offset :o",
                         s=status, l=limit, o=offset)
        return _rows(conn, "select id, agent_id, status, created_at, finished_at, attempts,"
                          " left(prompt, 160) as prompt_preview"
                          " from aihub.jobs order by created_at desc limit :l offset :o",
                    l=limit, o=offset)


def list_late(limit: int = 50) -> list[dict]:
    with _lock:
        conn = connect()
        return _rows(conn, "select id, job_id, attempt_id, attempt_status, received_at,"
                           " reviewed_at, note, payload"
                           " from aihub.late_results order by received_at desc limit :l", l=limit)


def count_jobs(status: str | None = None) -> int:
    with _lock:
        conn = connect()
        if status:
            return _one(conn, "select count(*) as n from aihub.jobs where status = :s", s=status)["n"]
        return _one(conn, "select count(*) as n from aihub.jobs")["n"]


def events(job_id: str) -> list[dict]:
    with _lock:
        conn = connect()
        return _rows(
            conn,
            "select event, attempt_id, at, payload from aihub.job_events"
            " where job_id = :j order by id",
            j=job_id,
        )


def lease_job(worker: str, lease_seconds: int = 300, agents: str = "") -> dict | None:
    """Атомарно берёт задание из очереди, создаёт попытку. None — очередь пуста."""
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            row = _one(
                conn,
                """with c as (
                       select id from aihub.jobs
                        where status = 'queued'
                          and (:agents = '' or agent_id = any(string_to_array(:agents, ',')))
                        order by created_at
                        limit 1
                        for update skip locked
                   )
                   update aihub.jobs j
                      set status = 'running',
                          started_at = now(),
                          lease_until = now() + (:lease || ' seconds')::interval,
                          worker = :w,
                          attempts = j.attempts + 1
                     from c where j.id = c.id
                   returning j.id, j.agent_id, j.prompt, j.timeout_s, j.attempts""",
                lease=str(int(lease_seconds)), w=worker, agents=agents,
            )
            if not row:
                conn.run("commit")
                return None
            attempt_id = uuid.uuid4().hex
            conn.run(
                "insert into aihub.job_attempts(id, job_id, attempt_no, worker, lease_until)"
                " values (:a, :j, :n, :w, now() + (:lease || ' seconds')::interval)",
                a=attempt_id, j=row["id"], n=row["attempts"], w=worker, lease=str(int(lease_seconds)),
            )
            conn.run(
                "insert into aihub.job_events(job_id, attempt_id, event) values (:j, :a, 'leased')",
                j=row["id"], a=attempt_id,
            )
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return {**row, "attempt_id": attempt_id, "lease_seconds": lease_seconds}


def heartbeat(attempt_id: str, lease_seconds: int = 300) -> bool:
    with _lock:
        conn = connect()
        row = _one(
            conn,
            """update aihub.job_attempts a
                  set lease_until = now() + (:lease || ' seconds')::interval
                from aihub.jobs j
               where a.id = :a and a.status = 'running' and j.id = a.job_id and j.status = 'running'
               returning a.id""",
            a=attempt_id, lease=str(int(lease_seconds)),
        )
        return bool(row)


def _store_late(conn, job_id: str | None, attempt_id: str, attempt_status: str | None,
                ok: bool, output: str | None, error: str | None,
                usage: dict | None, elapsed: float | None) -> None:
    """Складывает неприменённый результат отдельно: он не теряется и не меняет статус задания."""
    payload = json.dumps({
        "ok": ok, "output": output, "error": error,
        "usage": usage or {}, "elapsed": elapsed,
    }, ensure_ascii=False)
    conn.run(
        "insert into aihub.late_results(job_id, attempt_id, attempt_status, payload)"
        " values (:j, :a, :s, :p::jsonb) on conflict (attempt_id) do nothing",
        j=job_id, a=attempt_id, s=attempt_status, p=payload,
    )


def finish_attempt(attempt_id: str, ok: bool, output: str | None, error: str | None,
                   usage: dict | None, elapsed: float | None,
                   job_id_hint: str | None = None) -> str:
    """Идемпотентно закрывает попытку.

    applied   — результат сохранён в задание;
    duplicate — эта попытка уже закрыта тем же результатом, повтор ничего не меняет;
    late      — аренда истекла (или задание уже закрыто иначе): результат сохранён в late_results;
    stale     — попытка не последняя, задание закрыто другой попыткой: результат в late_results;
    not_found — такой попытки нет.
    """
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            attempt = _one(conn, "select * from aihub.job_attempts where id = :a", a=attempt_id)
            if not attempt:
                conn.run("rollback")
                if job_id_hint:
                    _store_late(conn, job_id_hint, attempt_id, None, ok, output, error, usage, elapsed)
                    conn.run("commit")
                    return "late"
                return "not_found"
            if attempt["status"] in ("succeeded", "failed"):
                conn.run("rollback")
                return "duplicate"
            if attempt["status"] != "running":
                _store_late(conn, attempt["job_id"], attempt_id, attempt["status"],
                            ok, output, error, usage, elapsed)
                conn.run("commit")
                return "late"
            job = _one(conn, "select * from aihub.jobs where id = :j for update", j=attempt["job_id"])
            if not job or job["status"] != "running":
                _store_late(conn, attempt["job_id"], attempt_id,
                            attempt["status"], ok, output, error, usage, elapsed)
                conn.run("commit")
                return "stale"
            latest = _one(
                conn,
                "select max(attempt_no) as n from aihub.job_attempts where job_id = :j", j=job["id"],
            )
            if latest and latest["n"] != attempt["attempt_no"]:
                _store_late(conn, job["id"], attempt_id, attempt["status"],
                            ok, output, error, usage, elapsed)
                conn.run("commit")
                return "stale"
            status = "succeeded" if ok else "failed"
            conn.run(
                "update aihub.job_attempts set status = :s, finished_at = now(), output = :o,"
                " error = :e, usage = :u::jsonb where id = :a",
                s=status, o=output, e=error, u=json.dumps(usage or {}, ensure_ascii=False), a=attempt_id,
            )
            conn.run(
                "update aihub.jobs set status = :s, finished_at = now(), result_output = :o,"
                " result_error = :e, result_usage = :u::jsonb, lease_until = null,"
                " needs_reconcile = false where id = :j",
                s=status, o=output, e=error, u=json.dumps(usage or {}, ensure_ascii=False), j=job["id"],
            )
            conn.run(
                "insert into aihub.job_events(job_id, attempt_id, event, payload)"
                " values (:j, :a, :e, :p::jsonb)",
                j=job["id"], a=attempt_id, e=status,
                p=json.dumps({"elapsed": elapsed}, ensure_ascii=False),
            )
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return "applied"


def cancel_job(job_id: str) -> str:
    """queued → cancelled сразу; running → cancel_requested (подтверждает исполнитель)."""
    with _lock:
        conn = connect()
        conn.run("begin")
        try:
            job = _one(conn, "select * from aihub.jobs where id = :j for update", j=job_id)
            if not job:
                conn.run("rollback")
                return "not_found"
            if job["status"] in ("succeeded", "failed", "cancelled", "timeout"):
                conn.run("rollback")
                return "already_finished"
            if job["status"] == "queued":
                conn.run("update aihub.jobs set status = 'cancelled', finished_at = now() where id = :j", j=job_id)
                status = "cancelled"
            else:
                conn.run("update aihub.jobs set needs_reconcile = true where id = :j", j=job_id)
                status = "cancel_requested"
            conn.run(
                "insert into aihub.job_events(job_id, event) values (:j, :e)", j=job_id, e=status,
            )
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return status


def expire_leases() -> int:
    """Истёкшая аренда: попытка → unknown, задание → unknown (без автоповтора)."""
    with _lock:
        conn = connect()
        # Один оператор: попытка, задание и событие меняются вместе. Иначе результат,
        # принятый между UPDATE-ами, мог быть перетёрт статусом unknown.
        row = _one(
            conn,
            """with expired as (
                   update aihub.job_attempts a
                      set status = 'unknown', finished_at = now()
                     from aihub.jobs j
                    where a.status = 'running' and a.lease_until < now()
                      and j.id = a.job_id and j.status = 'running'
                   returning a.id, a.job_id
               ),
               jobs_upd as (
                   update aihub.jobs j
                      set status = 'unknown', needs_reconcile = true, finished_at = now()
                     from expired e
                    where j.id = e.job_id and j.status = 'running'
                   returning j.id
               ),
               ev as (
                   insert into aihub.job_events(job_id, attempt_id, event)
                   select e.job_id, e.id, 'lease_expired' from expired e
                   returning 1
               )
               select (select count(*) from expired) as n""",
        )
        return int(row["n"])