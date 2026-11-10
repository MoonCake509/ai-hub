"""CORE-03: immutable human review records for unknown and late executions."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime

from . import db

class CaseNotFound(Exception):
    pass


class DecisionConflict(Exception):
    pass


CASE_SQL = """
with latest_attempt as (
    select distinct on (job_id) job_id, id as attempt_id, attempt_no, status as attempt_status
      from aihub.job_attempts order by job_id, attempt_no desc
), sources as (
    select 'job:' || j.id as case_id, 'job'::text as source_type, null::bigint as late_result_id,
           j.id as job_id, j.project_id, j.kind, j.status as job_status,
           a.attempt_id, a.attempt_no, a.attempt_status,
           j.created_at as created_at, coalesce(j.finished_at, j.created_at) as event_at,
           j.prompt, j.idempotency_key, j.run_snapshot, j.result_output as artifact_output,
           j.result_error as artifact_error, null::jsonb as late_payload,
           a.job_id as attempt_job_id, j.id as linked_job_id
      from aihub.jobs j left join latest_attempt a on a.job_id = j.id
     where j.needs_reconcile = true or j.status = 'unknown'
    union all
    select 'late:' || l.id::text as case_id, 'late'::text as source_type, l.id as late_result_id,
           l.job_id, j.project_id, j.kind, j.status as job_status,
           a.id as attempt_id, a.attempt_no, a.status as attempt_status,
           j.created_at, l.received_at as event_at,
           j.prompt, j.idempotency_key, j.run_snapshot,
           null::text as artifact_output, null::text as artifact_error, l.payload as late_payload,
           a.job_id as attempt_job_id,
           case when a.job_id = l.job_id then j.id else null end as linked_job_id
      from aihub.late_results l
      left join aihub.job_attempts a on a.id = l.attempt_id
      left join aihub.jobs j on j.id = l.job_id
)
select s.*, case d.action
         when 'accept_historical' then 'accepted_historical'
         when 'reject' then 'rejected'
         else coalesce(d.action, 'unresolved') end as decision_state,
       coalesce(d.revision, 0) as revision, d.actor, d.reason, d.evidence as decision_evidence,
       d.created_at as decided_at
  from sources s left join aihub.workspace_reconciliation_audit d on d.case_id = s.case_id
"""


def _json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value is not None else None)


def _cursor_encode(row: dict) -> str:
    payload = json.dumps({"at": _iso(row["event_at"]), "case_id": row["case_id"]},
                         separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _cursor_decode(cursor: str | None) -> tuple[str | None, str | None]:
    if not cursor:
        return None, None
    if len(cursor) > 2048:
        raise ValueError("cursor too long")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("case_id"), str):
            raise ValueError
        datetime.fromisoformat(data["at"])
        if not (data["case_id"].startswith("job:") or data["case_id"].startswith("late:")):
            raise ValueError
        return data["at"], data["case_id"]
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("invalid cursor") from None


def _provenance(row: dict) -> tuple[dict, str | None]:
    payload = _json(row.get("late_payload"))
    prompt = _json(row.get("prompt"))
    envelope = prompt if prompt.get("kind") == "workspace" else {}
    op = envelope.get("op")
    args = envelope.get("args") if isinstance(envelope.get("args"), dict) else {}
    output_text = payload.get("output") if isinstance(payload.get("output"), str) else ""
    output = _json(output_text)
    attempt_id = row.get("attempt_id")
    job_id = row.get("linked_job_id")
    candidate = envelope.get("digest") if isinstance(envelope.get("digest"), str) else None
    expected_preimage = None
    applied_ok = False
    if row.get("source_type") == "late" and op == "apply_patch" and output_text:
        changes = args.get("changes") if isinstance(args.get("changes"), list) else []
        expected_preimage = hashlib.sha256(json.dumps(
            [{"path": c.get("path"), "sha256": c.get("preimage_sha256")} for c in changes],
            sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        expected_applied = []
        try:
            for change in changes:
                content_text = change.get("content_b64")
                if content_text is None:
                    action, file_sha = "delete", None
                else:
                    import base64 as _b64
                    content = _b64.b64decode(content_text, validate=True)
                    action = "update" if change.get("preimage_sha256") else "create"
                    file_sha = hashlib.sha256(content).hexdigest()
                expected_applied.append({"path": change.get("path"), "action": action,
                                         "sha256": file_sha})
            actual_applied = [{"path": x.get("path"), "action": x.get("action"),
                               "sha256": x.get("sha256")} for x in (output.get("applied") or [])]
            expected_root = str(args.get("root") or "").replace("/", "\\").rstrip("\\").casefold()
            output_root = str(output.get("root") or "").replace("/", "\\").rstrip("\\").casefold()
            applied_ok = (
                payload.get("ok") is True and payload.get("usage", {}).get("workspace_op") == op
                and row.get("attempt_job_id") == row.get("job_id") == job_id
                and output.get("job_id") == row.get("job_id")
                and output.get("attempt_id") == attempt_id
                and output.get("operation") == op
                and output.get("candidate_digest") == candidate
                and output.get("base_tree_digest") == args.get("base_tree_digest")
                and output.get("preimage_digest") == expected_preimage
                and output_root == expected_root
                and isinstance(output.get("tree_digest"), str)
                and len(output["tree_digest"]) == 64
                and actual_applied == expected_applied
            )
        except (ValueError, TypeError, AttributeError):
            applied_ok = False
    receipt = None
    if applied_ok:
        with db._lock:
            conn = db.connect()
            receipt = db._one(conn, db._qualify(
                "select idempotency_key, op, host_id, job_id, args_digest, status,"
                " result_digest, attempt_id from aihub.workspace_receipts"
                " where idempotency_key = :k"), k=row.get("idempotency_key"))
    expected_output_digest = _sha(output_text) if output_text else None
    linked = bool(
        applied_ok and receipt
        and receipt.get("op") == op
        and receipt.get("job_id") == row.get("job_id")
        and receipt.get("args_digest") == candidate
        and receipt.get("attempt_id") == attempt_id
        # `unknown` is the immutable receipt for the original uncertain
        # attempt. A later artifact is kept in late_results; it can be linked
        # through the same exact attempt/job/key/op/candidate identity without
        # rewriting that first receipt or pretending its digest covered the
        # later artifact.
        and receipt.get("status") in {"unknown", "late"}
    )
    evidence = {
        "provenance": "linked_worker_artifact" if linked else "insufficient",
        "attempt_id": attempt_id,
        "job_id": job_id,
        "operation_key": row.get("idempotency_key"),
        "operation": op,
        "candidate_digest": candidate,
        "tree_digest": output.get("tree_digest") if linked else None,
        "preimage_digest": expected_preimage if linked else None,
        "artifact_digest": expected_output_digest,
        "receipt_status": receipt.get("status") if linked else None,
        "receipt_result_digest": receipt.get("result_digest") if linked else None,
    }
    reason = None if linked else (
        "No safe historical artifact is fully linked to the recorded attempt, job, workspace operation and input digest.")
    return evidence, reason


def _case(row: dict) -> dict:
    evidence, disabled_reason = _provenance(row)
    action = row.get("decision_state") or "unresolved"
    allowed = []
    if action == "unresolved":
        if evidence.get("provenance") == "linked_worker_artifact":
            allowed.append("accept_historical")
        allowed.append("reject")
    payload = _json(row.get("late_payload"))
    artifact_output = payload.get("output") if row.get("source_type") == "late" else row.get("artifact_output")
    return {
        "case_id": row["case_id"], "source": row.get("source_type"),
        "job_id": row.get("job_id"), "late_result_id": row.get("late_result_id"),
        "project_id": row.get("project_id"), "kind": row.get("kind"),
        "job_status": row.get("job_status"), "attempt_id": row.get("attempt_id"),
        "attempt_no": row.get("attempt_no"), "attempt_status": row.get("attempt_status"),
        "execution_state": row.get("job_status") or row.get("attempt_status") or "unknown",
        "decision_state": action, "revision": int(row.get("revision") or 0),
        "operation": evidence.get("operation"),
        "candidate_digest": evidence.get("candidate_digest"),
        "artifact_summary": {"ok": payload.get("ok") if payload else None,
                             "error": payload.get("error") or row.get("artifact_error"),
                             "output_preview": str(artifact_output or "")[:1200],
                             "artifact_digest": evidence.get("artifact_digest")},
        "evidence": evidence, "allowed_actions": allowed,
        "disabled_reason": disabled_reason if "accept_historical" not in allowed else None,
        "received_at": _iso(row.get("event_at")), "created_at": _iso(row.get("created_at")),
    }


def list_cases(cursor: str | None = None, limit: int = 50,
               decision_state: str = "unresolved", job_status: str = "all") -> dict:
    if not 1 <= int(limit) <= 100:
        raise ValueError("limit must be 1..100")
    if decision_state not in {"unresolved", "accepted_historical", "rejected", "all"}:
        raise ValueError("invalid decision_state")
    allowed_status = {"queued", "running", "succeeded", "failed", "cancelled", "timeout",
                      "unknown", "cancel_requested", "all"}
    if job_status not in allowed_status:
        raise ValueError("invalid job_status")
    cursor_at, cursor_id = _cursor_decode(cursor)
    sql = db._qualify("select * from (" + CASE_SQL + ") s where " + """
 (:decision = 'all' or s.decision_state = :decision)
   and (:status = 'all' or coalesce(s.job_status, 'unknown') = :status)
   and (CAST(:cursor_at AS text) is null or (s.event_at, s.case_id) < (CAST(:cursor_at AS timestamptz), CAST(:cursor_id AS text)))
 order by s.event_at desc, s.case_id desc limit :n
""")
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, sql, decision=decision_state, status=job_status,
                        cursor_at=cursor_at, cursor_id=cursor_id, n=int(limit) + 1)
    more = len(rows) > int(limit)
    rows = rows[:int(limit)]
    items = [_case(row) for row in rows]
    return {"items": items, "next_cursor": _cursor_encode(rows[-1]) if more and rows else None}


def _load_case(conn, case_id: str) -> dict | None:
    if not isinstance(case_id, str) or not (case_id.startswith("job:") or case_id.startswith("late:")):
        return None
    row = db._one(conn, db._qualify("select * from (" + CASE_SQL + ") s where s.case_id = :case_id"),
                   case_id=case_id)
    return row


def get_case(case_id: str) -> dict | None:
    with db._lock:
        conn = db.connect()
        row = _load_case(conn, case_id)
        if not row:
            return None
        audit = db._rows(conn, db._qualify(
            "select case_id, revision, action, actor, reason, evidence, created_at"
            " from aihub.workspace_reconciliation_audit where case_id = :c order by revision"), c=case_id)
    result = _case(row)
    result["audit"] = [{**item, "created_at": _iso(item.get("created_at"))} for item in audit]
    return result


def decide(case_id: str, action: str, expected_revision: int, reason: str, actor: str) -> dict:
    if action not in {"accept_historical", "reject"}:
        raise ValueError("invalid action")
    reason = reason.strip() if isinstance(reason, str) else ""
    if not 10 <= len(reason) <= 1000:
        raise ValueError("reason must be 10..1000 characters")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    with db._lock:
        conn = db.connect()
        conn.run("begin")
        try:
            conn.run("select pg_advisory_xact_lock(hashtextextended(:c, 0))", c=case_id)
            row = _load_case(conn, case_id)
            if not row:
                raise CaseNotFound(case_id)
            current = db._one(conn, db._qualify(
                "select case_id, revision, action, actor, reason, evidence, created_at"
                " from aihub.workspace_reconciliation_audit where case_id = :c order by revision desc limit 1"), c=case_id)
            case = _case(row)
            if current:
                old_evidence = _json(current.get("evidence"))
                if (current.get("action") == action and current.get("actor") == actor
                        and current.get("reason") == reason and old_evidence == case["evidence"]):
                    conn.run("commit")
                    return {"case": case, "decision": {**current,
                            "created_at": _iso(current.get("created_at"))}}
                raise DecisionConflict("case already has a different terminal decision")
            if expected_revision != case["revision"]:
                raise DecisionConflict("stale revision")
            if action not in case["allowed_actions"]:
                raise DecisionConflict(case.get("disabled_reason") or "action is not allowed")
            row = db._one(conn, db._qualify(
                "insert into aihub.workspace_reconciliation_audit"
                "(case_id, revision, action, actor, reason, evidence)"
                " values (:c, 1, :a, :actor, :reason, :e::jsonb)"
                " returning case_id, revision, action, actor, reason, evidence, created_at"),
                c=case_id, a=action, actor=actor, reason=reason,
                e=json.dumps(case["evidence"], ensure_ascii=False, sort_keys=True))
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
    result_case = get_case(case_id)
    return {"case": result_case,
            "decision": {**row, "created_at": _iso(row.get("created_at"))}}
