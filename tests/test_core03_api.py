"""CORE-03 integration tests; requires the isolated core03 compose stack."""
import base64
import concurrent.futures
import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8202")
with open(os.environ["AIHUB_TOKEN_FILE"], encoding="utf-8") as fh:
    TOKEN = fh.read().strip()
ROOT = os.path.join(os.environ["CORE03_FIXTURE_ROOT"], "run-" + uuid.uuid4().hex[:8])
os.makedirs(ROOT)
with open(os.path.join(ROOT, "fixture.py"), "w", encoding="utf-8") as fh:
    fh.write("print('CORE03-MANAGED-OK')\n")
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "laptop"))
import workspace_host


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    request.add_header("Authorization", "Bearer " + TOKEN)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def sql(query):
    result = subprocess.run([
        "docker", "exec", "aihub-core03-postgres-1", "psql", "-U", "aihub_core03",
        "-d", "aihub_core03", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", query,
    ], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def create_project():
    code, body = api("POST", "/projects", {
        "name": "core03-test-" + uuid.uuid4().hex[:8], "host_id": "laptop", "workdir": ROOT,
        "settings": {"checks": {"smoke": {"cmd": ["python", "fixture.py"], "timeout_s": 30}}},
    })
    assert code in (200, 201), (code, body)
    return body.get("id") or body.get("project", {}).get("id")


def create_job(op, args, project_id, key=None):
    return api("POST", "/workspace/jobs", {
        "op": op, "args": dict(args, root=ROOT), "host_id": "laptop",
        "project_id": project_id, "idempotency_key": key or uuid.uuid4().hex,
    })


def lease(job_id):
    code, job = api("GET", "/bridge/jobs?worker=core03-test&agents=workspace&protocol=2&kinds=workspace")
    assert code == 200 and job.get("id") == job_id, (code, job)
    return job


def result(lease_row, ok=True, output=None, error=None, uncertain=False):
    return api("POST", "/bridge/result", {
        "job_id": lease_row["id"], "attempt_id": lease_row["attempt_id"], "ok": ok,
        "output": output, "error": error, "usage": {"workspace_op": "apply_patch"},
        "elapsed": 0.1, "uncertain": uncertain,
    })


def main():
    code, health = api("GET", "/health")
    assert code == 200 and health.get("build_sha"), health
    sql("update aihub.jobs set status='cancelled', finished_at=now() where status='queued' and kind='workspace' and run_snapshot->>'op' is not null and prompt like '%" + ROOT + "%'")
    project_id = create_project()
    assert project_id
    base_tree = workspace_host.tree_digest(ROOT)
    code, created = create_job("apply_patch", {
        "base_tree_digest": base_tree,
        "changes": [{"path": "created.txt", "preimage_sha256": None,
                     "content_b64": base64.b64encode(b"core03").decode()}],
    }, project_id, "core03-idem-" + uuid.uuid4().hex)
    assert code == 200 and not created["reused"], (code, created)
    job_id, key = created["job_id"], created["idempotency_key"]
    code, conflict = create_job("apply_patch", {
        "base_tree_digest": base_tree,
        "changes": [{"path": "different.txt", "preimage_sha256": None,
                     "content_b64": base64.b64encode(b"different").decode()}],
    }, project_id, key)
    assert code == 409 and conflict["error"]["code"] == "idempotency_conflict", (code, conflict)
    leased = lease(job_id)
    generation = leased["resource"]["generation"]
    code, bad_hb = api("POST", "/bridge/heartbeat", {
        "attempt_id": leased["attempt_id"], "resource_generation": generation + 1})
    assert code == 200 and bad_hb["ok"] is False, bad_hb
    code, good_hb = api("POST", "/bridge/heartbeat", {
        "attempt_id": leased["attempt_id"], "resource_generation": generation})
    assert code == 200 and good_hb["ok"] is True and good_hb["resource"]["generation"] == generation, good_hb
    artifact = {
        "root": ROOT, "operation": "apply_patch", "job_id": job_id,
        "attempt_id": leased["attempt_id"], "candidate_digest": created["digest"],
        "base_tree_digest": base_tree, "tree_digest": "a" * 64,
        "preimage_digest": hashlib.sha256(json.dumps(
            [{"path": "created.txt", "sha256": None}], sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        "applied": [{"path": "created.txt", "action": "create",
                     "sha256": hashlib.sha256(b"core03").hexdigest()}],
    }
    code, unknown = result(leased, False, json.dumps({"effect_uncertain": True}), "lost", True)
    assert code == 200 and unknown["outcome"] == "unknown", (code, unknown)
    code, duplicate = result(leased, False, json.dumps({"effect_uncertain": True}), "lost", True)
    assert code == 200 and duplicate["outcome"] == "duplicate", (code, duplicate)
    code, job = api("GET", "/workspace/jobs/" + job_id)
    assert job["status"] == "unknown" and job["receipt"]["status"] == "unknown", job
    code, late = result(leased, True, json.dumps(artifact))
    assert code == 200 and late["outcome"] == "late", (code, late)
    code, late_again = result(leased, True, json.dumps(artifact))
    assert code == 200 and late_again["outcome"] == "late", (code, late_again)
    code, immutable = api("GET", "/workspace/jobs/" + job_id)
    assert immutable["status"] == "unknown" and immutable["receipt"]["status"] == "unknown", immutable
    assert sql("select count(*) from aihub.late_results where attempt_id='%s'" % leased["attempt_id"]) == "1"

    code, first = api("GET", "/workspace/reconciliation/cases?limit=1&decision_state=unresolved")
    assert code == 200 and len(first["items"]) == 1 and first["next_cursor"], (code, first)
    code, second = api("GET", "/workspace/reconciliation/cases?limit=100&decision_state=unresolved&cursor=" + first["next_cursor"])
    assert code == 200 and all(x["case_id"] != first["items"][0]["case_id"] for x in second["items"]), second
    code, bad_cursor = api("GET", "/workspace/reconciliation/cases?cursor=not-a-cursor")
    assert code == 400, bad_cursor
    code, all_cases = api("GET", "/workspace/reconciliation/cases?limit=100&decision_state=unresolved")
    late_case = next(x for x in all_cases["items"] if x["source"] == "late" and x["job_id"] == job_id)
    assert late_case["evidence"]["provenance"] == "linked_worker_artifact", late_case
    assert "accept_historical" in late_case["allowed_actions"], late_case
    code, detail = api("GET", "/workspace/reconciliation/cases/" + late_case["case_id"])
    assert code == 200 and detail["audit"] == [] and detail["revision"] == 0, detail
    decision = {"action": "accept_historical", "expected_revision": 0,
                "reason": "verified immutable worker artifact", "actor": "spoofed-client"}
    code, decided = api("POST", "/workspace/reconciliation/cases/" + late_case["case_id"] + "/decision", decision)
    assert code == 200 and decided["decision"]["actor"] == "hub-token-holder", (code, decided)
    code, repeated = api("POST", "/workspace/reconciliation/cases/" + late_case["case_id"] + "/decision", decision)
    assert code == 200 and repeated["decision"]["revision"] == 1, (code, repeated)
    code, stale = api("POST", "/workspace/reconciliation/cases/" + late_case["case_id"] + "/decision",
                      dict(decision, action="reject"))
    assert code == 409, stale

    job_case = next(x for x in all_cases["items"] if x["case_id"] == "job:" + job_id)
    race_path = "/workspace/reconciliation/cases/" + job_case["case_id"] + "/decision"
    decisions = [
        {"action": "reject", "expected_revision": 0, "reason": "race decision candidate one"},
        {"action": "reject", "expected_revision": 0, "reason": "race decision candidate two"},
    ]
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        raced = list(pool.map(lambda body: api("POST", race_path, body), decisions))
    assert sorted(code for code, _ in raced) == [200, 409], raced
    code, final = api("GET", "/workspace/reconciliation/cases/" + job_case["case_id"])
    assert code == 200 and len(final["audit"]) == 1 and final["decision_state"] == "rejected", final
    assert sql("select lease_until > now() from aihub.resource_leases where job_id='%s'" % job_id) == "t"
    print("CORE-03 API integration: PASS")


if __name__ == "__main__":
    main()
