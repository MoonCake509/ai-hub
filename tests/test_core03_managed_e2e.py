"""Managed fixture through the real isolated stage worker and Docker runner."""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AIHUB_BASE", "http://127.0.0.1:8202")
with open(os.environ["AIHUB_TOKEN_FILE"], encoding="utf-8") as fh:
    TOKEN = fh.read().strip()
ROOT = os.path.join(os.environ["CORE03_FIXTURE_ROOT"], "managed-" + uuid.uuid4().hex[:8])
os.makedirs(ROOT)
with open(os.path.join(ROOT, "fixture.py"), "w", encoding="utf-8") as fh:
    fh.write("print('CORE03-MANAGED-OK')\n")


def api(method, path, body=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method)
    request.add_header("Authorization", "Bearer " + TOKEN)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def create(project, op, args):
    code, body = api("POST", "/workspace/jobs", {
        "op": op, "args": dict(args, root=ROOT), "host_id": "laptop", "project_id": project,
        "idempotency_key": uuid.uuid4().hex})
    assert code == 200, (code, body)
    return body


def wait(job_id):
    deadline = time.time() + 60
    while time.time() < deadline:
        code, body = api("GET", "/workspace/jobs/" + job_id)
        assert code == 200, body
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.25)
    raise AssertionError("managed stage job timeout: " + job_id)


def main():
    code, project = api("POST", "/projects", {
        "name": "core03-managed-" + uuid.uuid4().hex[:8], "host_id": "laptop", "workdir": ROOT,
        "settings": {"checks": {"smoke": {"cmd": ["python", "fixture.py"], "timeout_s": 30}}}})
    assert code in (200, 201), (code, project)
    project_id = project.get("id") or project.get("project", {}).get("id")
    inspected = wait(create(project_id, "inspect", {})["job_id"])
    assert inspected["status"] == "succeeded", inspected
    snapshot = wait(create(project_id, "snapshot", {"label": "core03-managed"})["job_id"])
    assert snapshot["status"] == "succeeded", snapshot
    checked = wait(create(project_id, "run_checks", {
        "snapshot_id": snapshot["result"]["snapshot_id"], "checks": ["smoke"]})["job_id"])
    assert checked["status"] == "succeeded" and checked["result"]["ok"] is True, checked
    assert "CORE03-MANAGED-OK" in checked["result"]["checks"][0]["stdout_tail"], checked
    patched = wait(create(project_id, "apply_patch", {
        "base_tree_digest": inspected["result"]["tree_digest"],
        "changes": [{"path": "managed.txt", "preimage_sha256": None,
                     "content_b64": "bWFuYWdlZA=="}]})["job_id"])
    assert patched["status"] == "succeeded" and patched["receipt"]["status"] == "applied", patched
    with open(os.path.join(ROOT, "managed.txt"), encoding="utf-8") as fh:
        assert fh.read() == "managed"
    print("CORE-03 managed worker + isolated Docker fixture: PASS")


if __name__ == "__main__":
    main()
