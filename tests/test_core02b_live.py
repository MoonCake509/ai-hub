"""CORE-02b: живой сквозной контракт операций workspace на устройстве воркера.

Скрипт запускается на сервере и говорит с хабом по HTTP; операции исполняет
ноутбук (воркер DESKTOP-BHMJN55) на своём диске. Проверяются: inspect,
list_files, read_files, snapshot, run_checks в изолированной копии, apply_patch с
preimage и повтор по idempotency key, package, отказы по схеме и пути.
"""
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("HUB_URL", "http://127.0.0.1:8000").rstrip("/")
ROOT = "C:/Users/lavy/aihub-ws-fixture"
ROOT2 = "C:/Users/lavy/aihub-ws-f2"
PASSED, FAILED = [], []
RUN = str(int(time.time()))  # ключи идемпотентности уникальны для прогона


def token() -> str:
    env = os.path.join("R:/AI-Hub/infra", ".env")
    with open(env, encoding="utf-8") as fh:
        for line in fh:
            if line.strip().startswith("HUB_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


TOKEN = token()


def call(method: str, path: str, body: dict | None = None, timeout: float = 60):
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
            return exc.code, {"raw": raw[:400]}


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print("ok   " + name)
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"FAIL {name}: {type(exc).__name__}: {exc}")


def op(op_name: str, args: dict, project_id: str | None = None, key: str | None = None,
       host: str = "laptop", wait_s: float = 120) -> dict:
    body = {"op": op_name, "args": args, "host_id": host}
    if project_id:
        body["project_id"] = project_id
    if key:
        body["idempotency_key"] = key
    status, resp = call("POST", "/workspace/jobs", body)
    if status != 200:
        raise AssertionError(f"create {op_name}: http {status} {resp}")
    job_id = resp["job_id"]
    deadline = time.time() + wait_s
    while time.time() < deadline:
        status, job = call("GET", f"/workspace/jobs/{job_id}")
        if status != 200:
            raise AssertionError(f"get {job_id}: http {status} {job}")
        if job["status"] in ("succeeded", "failed", "timeout", "cancelled"):
            job["_create"] = resp
            return job
        time.sleep(1.0)
    raise AssertionError(f"job {job_id} не завершился за {wait_s}s")


def ok_result(job: dict) -> dict:
    assert job["status"] == "succeeded", f"статус {job['status']}: {job.get('error')}"
    assert job["result"], "нет разобранного результата"
    return job["result"]


def ensure_project(name: str, workdir: str, settings: dict) -> str:
    status, body = call("GET", "/projects")
    assert status == 200, (status, body)
    for proj in body.get("projects", []):
        if proj.get("workdir") == workdir and proj.get("host_id") == "laptop":
            return proj["id"]
    status, proj = call("POST", "/projects", {
        "name": name, "host_id": "laptop", "workdir": workdir, "settings": settings})
    assert status == 200, (status, proj)
    return proj["id"]


PROJECT = None
STATE = {}


def setup() -> str:
    global PROJECT
    PROJECT = ensure_project("CORE-02b fixture", ROOT, {
        "checks": {
            "unit": {"cmd": ["python", "-X", "utf8", "tests/test_discount.py"], "timeout_s": 180},
        }})
    return PROJECT


def test_inspect():
    result = ok_result(op("inspect", {"root": ROOT}, PROJECT))
    assert len(result["head"]) == 40, result.get("head")
    assert result["tree_digest"] and len(result["tree_digest"]) == 64
    assert result["files"] >= 3, result["files"]
    assert result["is_git"] is True
    assert result["host"] == "DESKTOP-BHMJN55", result["host"]
    STATE["tree"] = result["tree_digest"]
    STATE["head"] = result["head"]


def test_list_files_hides_secret():
    result = ok_result(op("list_files", {"root": ROOT, "glob": "**/*.py"}, PROJECT))
    names = [item["path"] for item in result["items"]]
    assert "discount.py" in names, names
    assert "tests/test_discount.py" in names, names
    assert not any("token" in n.lower() for n in names), names


def test_read_files():
    result = ok_result(op("read_files", {"root": ROOT, "files": [{"path": "discount.py"}]}, PROJECT))
    item = result["files"][0]
    assert "def final_price" in item["text"], item["text"][:120]
    STATE["discount_sha"] = item["sha256"]


def test_read_env_rejected():
    status, body = call("POST", "/workspace/jobs", {
        "op": "read_files", "args": {"root": ROOT, "files": [{"path": ".env"}]},
        "host_id": "laptop", "project_id": PROJECT})
    assert status == 400, (status, body)
    assert body["error"]["code"] == "denied_path", body


def test_root_outside_project_rejected():
    status, body = call("POST", "/workspace/jobs", {
        "op": "inspect", "args": {"root": "C:/Windows"}, "host_id": "laptop",
        "project_id": PROJECT})
    assert status == 400, (status, body)
    assert body["error"]["code"] == "root_not_allowed", body


def test_host_without_capability_rejected():
    status, body = call("POST", "/workspace/jobs", {
        "op": "inspect", "args": {"root": ROOT}, "host_id": "server"})
    assert status == 400, (status, body)
    assert body["error"]["code"] in ("unsupported_op", "no_roots"), body


def test_snapshot_matches_tree():
    result = ok_result(op("snapshot", {"root": ROOT, "label": "live"}, PROJECT))
    assert result["tree_digest"] == STATE["tree"], (result["tree_digest"], STATE["tree"])
    assert result["files"] >= 3
    STATE["snap"] = result["snapshot_id"]


def test_checks_fail_before_fix():
    result = ok_result(op("run_checks", {"root": ROOT, "snapshot_id": STATE["snap"],
                                         "checks": ["unit"]}, PROJECT))
    item = result["checks"][0]
    assert item["passed"] is False, item
    assert item["exit_code"] != 0, item
    assert "FAIL" in (item["stdout_tail"] or ""), item["stdout_tail"][:200]
    assert result["ok"] is False


FIXED_LINES = [
    '"""Учебный модуль: расчёт скидки с проверкой границ."""',
    "",
    "",
    "def final_price(amount: int, percent: int = 10) -> float:",
    "    if amount < 0:",
    '        raise ValueError("amount must be >= 0")',
    "    if not 0 <= percent <= 100:",
    '        raise ValueError("percent must be in 0..100")',
    "    return amount - amount * percent / 100.0",
    "",
]
FIXED_BODY = chr(10).join(FIXED_LINES)

def patch_args(base: str, body: str, preimage: str | None) -> dict:
    return {"root": ROOT, "base_tree_digest": base, "changes": [{
        "path": "discount.py", "preimage_sha256": preimage,
        "content_b64": base64.b64encode(body.encode("utf-8")).decode("ascii")}]}


def test_apply_patch_updates_file():
    job = op("apply_patch", patch_args(STATE["tree"], FIXED_BODY, STATE["discount_sha"]),
             PROJECT, key="core02b-live-patch-" + RUN)
    result = ok_result(job)
    assert result["applied"][0]["action"] == "update", result["applied"]
    assert result["tree_digest"] != STATE["tree"]
    assert result["backup_dir"], "нет каталога бэкапа preimage"
    STATE["tree_after"] = result["tree_digest"]
    STATE["patch_job"] = job["id"]


def test_patch_replay_is_same_job():
    job = op("apply_patch", patch_args(STATE["tree"], FIXED_BODY, STATE["discount_sha"]),
             PROJECT, key="core02b-live-patch-" + RUN)
    assert job["id"] == STATE["patch_job"], (job["id"], STATE["patch_job"])
    create = job.get("_create") or {}
    assert create.get("reused") is True, create
    receipt = job.get("receipt") or {}
    assert receipt.get("status") == "applied", receipt


def test_file_really_changed():
    result = ok_result(op("read_files", {"root": ROOT, "files": [{"path": "discount.py"}]}, PROJECT))
    text = result["files"][0]["text"]
    assert "amount must be >= 0" in text, text[:200]
    assert hashlib.sha256(FIXED_BODY.encode("utf-8")).hexdigest() == result["files"][0]["sha256"]


def test_stale_base_rejected():
    job = op("apply_patch", patch_args(STATE["tree"], FIXED_BODY, STATE["discount_sha"]),
             PROJECT, key="core02b-live-patch-stale-" + RUN)
    assert job["status"] == "failed", job["status"]
    assert "tree_changed" in (job.get("error") or ""), job.get("error")


def test_checks_pass_after_fix():
    snap = ok_result(op("snapshot", {"root": ROOT, "label": "after"}, PROJECT))
    result = ok_result(op("run_checks", {"root": ROOT, "snapshot_id": snap["snapshot_id"],
                                         "checks": ["unit"]}, PROJECT))
    item = result["checks"][0]
    assert item["passed"] is True, item
    assert result["ok"] is True
    STATE["snap2"] = snap["snapshot_id"]
    STATE["tree_fixed_snapshot"] = snap["tree_digest"]


def test_package_bundle():
    result = ok_result(op("package", {"root": ROOT, "snapshot_id": STATE["snap2"],
                                      "label": "review"}, PROJECT))
    bundle = result["bundle"]
    assert len(bundle["manifest_digest"]) == 64
    assert bundle["files"] >= 2, bundle
    paths = [item["path"] for item in result["manifest"]]
    assert "discount.py" in paths, paths
    STATE["bundle"] = bundle


def test_op_catalog_lists_all():
    status, body = call("GET", "/workspace/ops")
    assert status == 200
    names = {op_item["name"] for op_item in body["ops"]}
    assert names == {"inspect", "snapshot", "list_files", "read_files",
                     "apply_patch", "run_checks", "package"}, names


def main() -> int:
    setup()
    order = [
        "test_op_catalog_lists_all",
        "test_inspect",
        "test_list_files_hides_secret",
        "test_read_files",
        "test_read_env_rejected",
        "test_root_outside_project_rejected",
        "test_host_without_capability_rejected",
        "test_snapshot_matches_tree",
        "test_checks_fail_before_fix",
        "test_apply_patch_updates_file",
        "test_patch_replay_is_same_job",
        "test_file_really_changed",
        "test_stale_base_rejected",
        "test_checks_pass_after_fix",
        "test_package_bundle",
    ]
    for name in order:
        check(name[5:], globals()[name])
    print()
    print(f"влазит {len(PASSED)}/{len(order)}")
    print("проект:", PROJECT, "бандл:", (STATE.get("bundle") or {}).get("path"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
