#!/usr/bin/env python3
"""Single-purpose worker for the isolated CORE03 stage; never polls production."""
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agents", "laptop"))
import workspace_host

BASE = os.environ["CORE03_HUB_URL"].rstrip("/")
with open(os.environ["CORE03_TOKEN_FILE"], encoding="utf-8") as fh:
    TOKEN = fh.read().strip()
WORKER = "core03-stage"


def http(method, path, body=None):
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method)
    request.add_header("Authorization", "Bearer " + TOKEN)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def beat():
    capabilities = workspace_host.capabilities()
    return http("POST", "/bridge/worker/heartbeat", {
        "worker_id": WORKER, "protocol": 2, "host": "core03-stage", "hb_interval_s": 1,
        "agents": [{"id": "codex", "ready": True, "capabilities": capabilities}],
        "capabilities": capabilities,
    })


def heartbeat(attempt_id, generation, stop):
    while not stop.wait(1):
        http("POST", "/bridge/heartbeat", {"attempt_id": attempt_id, "resource_generation": generation})


def execute(job):
    generation = (job.get("resource") or {}).get("generation")
    stop = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(job["attempt_id"], generation, stop), daemon=True)
    thread.start()

    def fence():
        code, response = http("POST", "/bridge/heartbeat", {
            "attempt_id": job["attempt_id"], "resource_generation": generation})
        resource = response.get("resource") or {}
        return code == 200 and response.get("ok") is True and resource.get("generation") == generation

    try:
        print(json.dumps({"job_id": job["id"], "stage": "started"}), flush=True)
        payload = workspace_host.run_job(job, fence_check=fence,
            hub_identity=hashlib.sha256(BASE.lower().encode()).hexdigest())
        print(json.dumps({"job_id": job["id"], "stage": "executed"}), flush=True)
    finally:
        stop.set()
        thread.join(timeout=2)
    print(json.dumps({"job_id": job["id"], "ok": payload.get("ok"), "error": payload.get("error")}), flush=True)
    code, response = http("POST", "/bridge/result", payload)
    if code != 200 or not response.get("recorded"):
        raise RuntimeError("stage result not recorded: %r %r" % (code, response))
    if (payload.get("usage") or {}).get("workspace_op") == "apply_patch":
        workspace_host.mark_effect_reported(job["id"], job["attempt_id"],
            hashlib.sha256(BASE.lower().encode()).hexdigest())


def main():
    code, response = beat()
    assert code == 200 and response.get("ok"), response
    once = "--once" in sys.argv
    while True:
        try:
            code, response = http("GET", "/bridge/jobs?worker=core03-stage&agents=workspace&protocol=2&kinds=workspace")
            if code == 200 and response.get("id"):
                execute(response)
                beat()
            elif once:
                return
        except (OSError, ValueError) as exc:
            if once:
                raise
            print("transient stage hub error: " + type(exc).__name__, flush=True)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
