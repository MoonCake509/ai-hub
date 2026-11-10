"""Persistence helpers for the opt-in channel-quality slice.

All tables live under the active AI Hub schema and use the existing db connection/lock.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from . import db

DDL = [
    """create table if not exists aihub.quality_runs (
        id text primary key,
        idempotency_key text not null unique,
        suite_id text not null,
        suite_version text not null,
        grader_version text not null,
        suite_snapshot jsonb not null,
        channel_id text not null,
        agent_id text not null,
        profile text,
        model text,
        case_count int not null,
        timeout_s int not null,
        status text not null default 'starting',
        created_at timestamptz not null default now(),
        started_at timestamptz,
        finished_at timestamptz,
        passed_count int,
        failed_count int,
        score double precision,
        queue_wait_seconds double precision,
        execution_seconds double precision,
        wall_seconds double precision,
        total_usage jsonb,
        error text
    )""",
    """create index if not exists quality_runs_created
       on aihub.quality_runs(created_at desc)""",
    """create table if not exists aihub.quality_run_items (
        id text primary key,
        run_id text not null references aihub.quality_runs(id) on delete cascade,
        ordinal int not null,
        case_id text not null,
        case_snapshot jsonb not null,
        job_id text unique,
        status text not null default 'pending',
        job_status text,
        passed boolean,
        grader_reason text,
        raw_response text,
        reported_model text,
        identity_evidence_source text,
        error text,
        first_started_at timestamptz,
        queue_wait_seconds double precision,
        execution_seconds double precision,
        wall_seconds double precision,
        usage jsonb,
        cost numeric,
        first_token_latency_seconds double precision,
        created_at timestamptz not null default now(),
        finished_at timestamptz,
        unique(run_id, case_id)
    )""",
    "alter table aihub.quality_run_items add column if not exists reported_model text",
    "alter table aihub.quality_run_items add column if not exists identity_evidence_source text",
    "alter table aihub.quality_run_items add column if not exists first_started_at timestamptz",
    # Q-01: additive only. Existing extended runs remain valid with NULL
    # budget fields; do not infer that their per-job timeout was a wall cap.
    "alter table aihub.quality_runs add column if not exists mode text",
    "alter table aihub.quality_runs add column if not exists budget_s int",
    "alter table aihub.quality_runs add column if not exists max_calls int",
    "alter table aihub.quality_runs add column if not exists budget_exhausted boolean not null default false",
    """create index if not exists quality_items_run_order
       on aihub.quality_run_items(run_id, ordinal)""",
    """create table if not exists aihub.quality_channels (
        channel_id text primary key,
        agent_id text not null,
        profile text,
        model text,
        enabled boolean not null default true,
        reason text not null default '',
        updated_at timestamptz not null default now()
    )""",
    """create table if not exists aihub.quality_channel_audit (
        id bigserial primary key,
        channel_id text not null,
        agent_id text not null,
        profile text,
        model text,
        enabled_before boolean,
        enabled_after boolean not null,
        reason text not null,
        changed_at timestamptz not null default now()
    )""",
    """create index if not exists quality_channel_audit_recent
       on aihub.quality_channel_audit(channel_id, changed_at desc)""",
]

JSON_FIELDS = {"suite_snapshot", "case_snapshot", "usage", "total_usage"}


def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return row
    for key in JSON_FIELDS:
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key])
            except (TypeError, ValueError):
                row[key] = None
    return row


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def init() -> None:
    with db._lock:
        conn = db.connect()
        for stmt in DDL:
            conn.run(db._qualify(stmt))


def create_run(run: dict[str, Any], cases: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    run_id = uuid.uuid4().hex
    with db._lock:
        conn = db.connect()
        conn.run("begin")
        try:
            existing = db._one(conn,
                "select * from aihub.quality_runs where idempotency_key = :k for update",
                k=run["idempotency_key"])
            if existing:
                conn.run("commit")
                return _decode(existing), False
            conn.run(db._qualify(
                "insert into aihub.quality_runs(id, idempotency_key, suite_id, suite_version, grader_version,"
                " suite_snapshot, channel_id, agent_id, profile, model, case_count, timeout_s, status,"
                " mode, budget_s, max_calls)"
                " values (:i,:k,:si,:sv,:gv,:ss::jsonb,:ch,:a,:p,:m,:n,:t,'starting',:mode,:budget,:calls)"),
                i=run_id, k=run["idempotency_key"], si=run["suite_id"], sv=run["suite_version"],
                gv=run["grader_version"], ss=_json(run["suite_snapshot"]), ch=run["channel_id"],
                a=run["agent_id"], p=run.get("profile"), m=run.get("model"),
                n=run["case_count"], t=run["timeout_s"], mode=run.get("mode") or "extended",
                budget=run.get("budget_s"), calls=run.get("max_calls") or run["case_count"])
            for index, case in enumerate(cases):
                conn.run(db._qualify(
                    "insert into aihub.quality_run_items(id,run_id,ordinal,case_id,case_snapshot)"
                    " values (:i,:r,:o,:c,:s::jsonb)"),
                    i=uuid.uuid4().hex, r=run_id, o=index, c=case["id"], s=_json(case))
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return get_run(run_id), True


def get_run(run_id: str) -> dict[str, Any] | None:
    with db._lock:
        conn = db.connect()
        run = _decode(db._one(conn, "select * from aihub.quality_runs where id=:i", i=run_id))
        if not run:
            return None
        items = db._rows(conn,
            "select * from aihub.quality_run_items where run_id=:i order by ordinal", i=run_id)
        run["items"] = [_decode(item) for item in items]
        return run


def get_run_by_key(key: str) -> dict[str, Any] | None:
    with db._lock:
        conn = db.connect()
        row = db._one(conn, "select id from aihub.quality_runs where idempotency_key=:k", k=key)
    return get_run(row["id"]) if row else None


def list_runs(limit: int = 30, offset: int = 0) -> list[dict[str, Any]]:
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn,
            "select * from aihub.quality_runs order by created_at desc limit :l offset :o",
            l=limit, o=offset)
        return [_decode(row) for row in rows]


def list_run_ids(limit: int = 100) -> list[str]:
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn,
            "select id from aihub.quality_runs where status in ('starting','queued','running')"
            " order by created_at limit :l", l=limit)
        return [row["id"] for row in rows]


def attach_job(item_id: str, job: dict[str, Any]) -> None:
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "update aihub.quality_run_items set job_id=:j,status=:s,job_status=:s where id=:i"),
            j=job["id"], s=job["status"], i=item_id)


def mark_item_enqueue_error(item_id: str, message: str) -> None:
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "update aihub.quality_run_items set status='failed',job_status='failed',passed=false,"
            "grader_reason=:e,error=:e,finished_at=now() where id=:i"), e=message, i=item_id)


def mark_item_skipped(item_id: str, state: str, message: str) -> None:
    if state not in ("not_applicable", "budget_exhausted"):
        raise ValueError("unsupported skipped state")
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "update aihub.quality_run_items set status=:s,job_status=null,passed=null,"
            "grader_reason=:e,error=null,finished_at=now() where id=:i"),
            s=state, e=message, i=item_id)


def item_by_job(job_id: str) -> dict[str, Any] | None:
    with db._lock:
        conn = db.connect()
        return _decode(db._one(conn,
            "select * from aihub.quality_run_items where job_id=:j", j=job_id))


def update_item(item_id: str, values: dict[str, Any]) -> None:
    json_keys = {"usage"}
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "update aihub.quality_run_items set status=:s,job_status=:js,passed=:p,"
            "grader_reason=:gr,raw_response=:raw,reported_model=:rm,identity_evidence_source=:ies,error=:e,first_started_at=:fs,queue_wait_seconds=:qw,"
            "execution_seconds=:ex,wall_seconds=:wall,usage=:u::jsonb,cost=:cost,"
            "first_token_latency_seconds=:ttft,finished_at=:finished where id=:i"),
            s=values.get("status"), js=values.get("job_status"), p=values.get("passed"),
            gr=values.get("grader_reason"), raw=values.get("raw_response"),
            rm=values.get("reported_model"), ies=values.get("identity_evidence_source"), e=values.get("error"),
            fs=values.get("first_started_at"),
            qw=values.get("queue_wait_seconds"), ex=values.get("execution_seconds"),
            wall=values.get("wall_seconds"), u=_json(values["usage"]) if values.get("usage") is not None else None,
            cost=values.get("cost"), ttft=values.get("first_token_latency_seconds"),
            finished=values.get("finished_at"), i=item_id)


def mark_budget_exhausted(run_id: str, reason: str) -> None:
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "update aihub.quality_runs set budget_exhausted=true,error=:e where id=:i"),
            e=reason, i=run_id)


def set_run_error(run_id: str, message: str) -> None:
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "update aihub.quality_runs set status='failed',error=:e,finished_at=now() where id=:i"),
            e=message, i=run_id)


def update_run_aggregate(run_id: str, values: dict[str, Any]) -> None:
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "update aihub.quality_runs set status=:s,started_at=:started,finished_at=:finished,"
            "passed_count=:passed,failed_count=:failed,score=:score,queue_wait_seconds=:qw,"
            "execution_seconds=:ex,wall_seconds=:wall,total_usage=:usage::jsonb,error=:error where id=:i"),
            s=values.get("status"), started=values.get("started_at"), finished=values.get("finished_at"),
            passed=values.get("passed_count"), failed=values.get("failed_count"), score=values.get("score"),
            qw=values.get("queue_wait_seconds"), ex=values.get("execution_seconds"),
            wall=values.get("wall_seconds"),
            usage=_json(values["total_usage"]) if values.get("total_usage") is not None else None,
            error=values.get("error"), i=run_id)


def get_channel(channel_id: str) -> dict[str, Any] | None:
    with db._lock:
        conn = db.connect()
        return db._one(conn, "select * from aihub.quality_channels where channel_id=:i", i=channel_id)


def list_channels() -> list[dict[str, Any]]:
    with db._lock:
        conn = db.connect()
        return db._rows(conn, "select * from aihub.quality_channels order by agent_id,profile,model")


def set_channel(channel: dict[str, Any], enabled: bool, reason: str) -> dict[str, Any]:
    with db._lock:
        conn = db.connect()
        conn.run("begin")
        try:
            old = db._one(conn,
                "select enabled,reason from aihub.quality_channels where channel_id=:i for update",
                i=channel["channel_id"])
            before = old["enabled"] if old else True
            conn.run(db._qualify(
                "insert into aihub.quality_channels(channel_id,agent_id,profile,model,enabled,reason,updated_at)"
                " values (:i,:a,:p,:m,:e,:r,now()) on conflict(channel_id) do update set"
                " enabled=excluded.enabled,reason=excluded.reason,updated_at=now()"),
                i=channel["channel_id"], a=channel["agent_id"], p=channel.get("profile"),
                m=channel.get("model"), e=enabled, r=reason)
            conn.run(db._qualify(
                "insert into aihub.quality_channel_audit(channel_id,agent_id,profile,model,enabled_before,enabled_after,reason)"
                " values (:i,:a,:p,:m,:before,:after,:reason)"),
                i=channel["channel_id"], a=channel["agent_id"], p=channel.get("profile"),
                m=channel.get("model"), before=before, after=enabled, reason=reason)
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
        return get_channel(channel["channel_id"]) or {}


def channel_audit(limit: int = 100) -> list[dict[str, Any]]:
    with db._lock:
        conn = db.connect()
        return db._rows(conn,
            "select * from aihub.quality_channel_audit order by changed_at desc,id desc limit :l", l=limit)
