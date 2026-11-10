"""CORE-02b: выборка задания под свободный checkout (живой контракт на проде).

Сценарий: голова очереди занята каталогом, который держит другое исполняемое
задание; хаб обязан отдать воркеру следующее задание со свободным каталогом,
а не пустой ответ. Задания — управляемые операции workspace: они несут проект и
рабочий каталог, то есть реально занимают checkout. Воркер ноутбука на время
теста остановлен, аренда и завершение делаются вручную через bridge API.
"""
import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
ROOT = "C:/Users/lavy/aihub-ws-fixture"
ROOT2 = "C:/Users/lavy/aihub-ws-f2"
PASSED, FAILED = [], []
RUN = str(int(time.time()))


def token():
    with open("R:/AI-Hub/infra/.env", encoding="utf-8") as fh:
        for line in fh:
            if line.strip().startswith("HUB_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


TOKEN = token()


def call(method, path, body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"raw": raw[:300]}


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print("ok   " + name)
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")


def project(name, workdir):
    status, body = call("GET", "/projects")
    for proj in body.get("projects", []):
        if proj.get("workdir") == workdir:
            return proj["id"]
    status, proj = call("POST", "/projects", {"name": name, "host_id": "laptop",
                                              "workdir": workdir, "settings": {}})
    assert status == 200, (status, proj)
    return proj["id"]


def patch_job(project_id, root, tag):
    status, body = call("POST", "/workspace/jobs", {
        "op": "apply_patch",
        "args": {"root": root, "base_tree_digest": "0" * 64, "changes": [
            {"path": "probe-%s.txt" % tag, "preimage_sha256": None,
             "content_b64": "cHJvYmU="}]},
        "host_id": "laptop", "project_id": project_id,
        "idempotency_key": "fair-%s-%s" % (tag, RUN)})
    assert status == 200, (status, body)
    return body["job_id"]


def lease(worker):
    """Аренда: непустой ответ — само задание, пустой — {"job": null}."""
    status, body = call("GET", f"/bridge/jobs?worker={worker}&agents=workspace"
                               f"&protocol=2&kinds=workspace")
    assert status == 200, (status, body)
    if body.get("id"):
        return body
    return None


def finish(job):
    call("POST", "/bridge/result", {"job_id": job["id"], "attempt_id": job["attempt_id"],
                                    "ok": False, "error": "probe finished"})


STATE = {}


def reset_queues():
    """Убирает хвосты прошлых прогонов fair-теста (метка fair-...)."""
    dropped = 0
    for status in ("queued", "running"):
        code, body = call("GET", f"/jobs?status={status}&limit=100")
        for job in (body.get("jobs") or []):
            preview = (job.get("prompt_preview") or "")
            if "probe-" in preview and (job.get("agent_id") == "workspace"):
                call("POST", f"/jobs/{job['id']}/cancel")
                dropped += 1
    code, body = call("GET", "/resources")
    for res in (body.get("resources") or []):
        if res.get("job_id"):
            call("POST", "/resources/release", {"key": res["key"], "job_id": res["job_id"]})
    return dropped


def test_blocked_head_does_not_stall_free_checkout():
    reset_queues()
    p1 = project("CORE-02b fair P1", ROOT)
    p2 = project("CORE-02b fair P2", ROOT2)

    holder = patch_job(p1, ROOT, "holder")
    leased = lease("probe-a")
    assert leased, "первое задание не арендовано"
    assert leased["id"] == holder, (leased["id"], holder)
    resource = leased.get("resource") or {}
    assert str(resource.get("key", "")).lower().startswith("checkout"), resource
    STATE["holder"] = leased

    blocked = patch_job(p1, ROOT, "blocked")
    free = patch_job(p2, ROOT2, "free")
    STATE["blocked"], STATE["free"] = blocked, free

    got = lease("probe-b")
    assert got, "хаб вернул пустую очередь при свободном задании в хвосте"
    assert got["id"] == free, (got["id"], free, blocked)
    STATE["got"] = got

    status, body = call("GET", "/jobs?status=queued&limit=100")
    queued = {item["id"] for item in (body.get("jobs") or [])}
    assert blocked in queued, "заблокированное задание потерялось из очереди"


def test_cleanup_releases_resources():
    if STATE.get("holder"):
        finish(STATE["holder"])
    if STATE.get("got"):
        finish(STATE["got"])
    for key in ("blocked", "free"):
        if STATE.get(key):
            call("POST", f"/jobs/{STATE[key]}/cancel")
    time.sleep(1)
    status, body = call("GET", "/resources")
    assert status == 200
    assert body.get("count") == 0, body


if __name__ == "__main__":
    check("blocked_head_does_not_stall_free_checkout",
          test_blocked_head_does_not_stall_free_checkout)
    check("cleanup_releases_resources", test_cleanup_releases_resources)
    print()
    print(f"влазит {len(PASSED)}/{len(PASSED) + len(FAILED)}")
    raise SystemExit(1 if FAILED else 0)
