"""CORE-02b: операции host worker на настоящем временном каталоге (Windows).

Каждый тест работает в своём каталоге: проверки не зависят от порядка запуска.
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workspace_host as host  # noqa: E402

PASSED, FAILED, DIRS = [], [], []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"{name}: {type(exc).__name__}: {exc}")


def expect(code, fn):
    try:
        fn()
    except host.OpFail as exc:
        assert exc.code == code, f"ожидался {code}, получен {exc.code}: {exc}"
        return
    raise AssertionError(f"ожидался отказ {code}")


def repo(with_git=True) -> str:
    root = tempfile.mkdtemp(prefix="wsops-")
    DIRS.append(root)
    os.environ["LOCALAPPDATA"] = root + "-state"
    os.makedirs(os.path.join(root, "pkg"), exist_ok=True)
    os.makedirs(os.path.join(root, "node_modules", "lib"), exist_ok=True)
    with open(os.path.join(root, "pkg", "calc.py"), "wb") as fh:
        fh.write(b"def add(a, b):\n    return a + b\n")
    with open(os.path.join(root, "README.md"), "wb") as fh:
        fh.write(b"# demo\n")
    with open(os.path.join(root, ".env"), "wb") as fh:
        fh.write(b"SECRET=1\n")
    with open(os.path.join(root, "node_modules", "lib", "x.js"), "wb") as fh:
        fh.write(b"console.log(1)\n")
    if with_git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=False)
        subprocess.run(["git", "add", "-A"], cwd=root, check=False)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t",
                        "commit", "-qm", "init"], cwd=root, check=False)
    return root


def sha(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def apply_patch(root: str, args: dict, job_id: str) -> dict:
    return host.op_apply_patch(root, args, job_id, job_id + "-attempt", 1,
                               lambda: True)


def test_inspect_git_repo():
    root = repo()
    info = host.op_inspect(root, {})
    assert info["exists"] and info["is_git"], info
    assert info["head"] and len(info["head"]) == 40, info["head"]
    assert info["files"] >= 2, info["files"]  # .env и node_modules скрыты
    assert "SECRET" not in json.dumps(info), "секрет попал в описание"


def test_inspect_plain_dir():
    root = repo(with_git=False)
    info = host.op_inspect(root, {})
    assert info["is_git"] is False
    assert info["head"] is None


def test_list_hides_secrets():
    root = repo()
    page = host.op_list_files(root, {"glob": "**/*", "limit": 100})
    names = [item["path"] for item in page["items"]]
    assert "pkg/calc.py" in names, names
    assert ".env" not in names, names
    assert not any("node_modules" in n for n in names), names


def test_list_pagination():
    root = repo()
    first = host.op_list_files(root, {"glob": "**/*", "limit": 1, "offset": 0})
    assert len(first["items"]) == 1
    assert first["next_offset"] == 1


def test_read_file_range():
    root = repo()
    out = host.op_read_files(root, {"files": [{"path": "pkg/calc.py", "offset": 0, "length": 20}]})
    item = out["files"][0]
    assert item["text"].startswith("def add"), item
    assert item["size"] > 20


def test_read_denied():
    root = repo()
    expect("denied_path", lambda: host.op_read_files(root, {"files": [{"path": ".env"}]}))


def test_read_traversal():
    root = repo()
    expect("traversal", lambda: host.op_read_files(root, {"files": [{"path": "../x.txt"}]}))


def test_read_missing():
    root = repo()
    expect("not_found", lambda: host.op_read_files(root, {"files": [{"path": "nope.txt"}]}))


def test_snapshot_is_separate_copy():
    root = repo()
    before = host.op_inspect(root, {})["tree_digest"]
    snap = host.op_snapshot(root, {"label": "unit"}, "jobsnapshot1")
    assert os.path.isdir(snap["path"])
    assert snap["tree_digest"] == before, (snap["tree_digest"], before)
    with open(os.path.join(snap["path"], "pkg", "calc.py"), "wb") as fh:
        fh.write(b"# changed in snapshot only\n")
    assert host.op_inspect(root, {})["tree_digest"] == before, "снимок изменил рабочий каталог"
    assert not os.path.exists(os.path.join(snap["path"], ".env")), "секрет попал в снимок"


def test_patch_wrong_base():
    root = repo()
    expect("tree_changed", lambda: apply_patch(root, {
        "base_tree_digest": "0" * 64,
        "changes": [{"path": "pkg/new.py", "preimage_sha256": None, "content_b64": "eA=="}]},
        "jobwrongbase"))


def test_patch_requires_preimage_for_existing():
    root = repo()
    digest = host.op_inspect(root, {})["tree_digest"]
    expect("preimage_missing", lambda: apply_patch(root, {
        "base_tree_digest": digest,
        "changes": [{"path": "pkg/calc.py", "preimage_sha256": None,
                     "content_b64": base64.b64encode(b"x").decode()}]}, "jobreuse1"))


def test_patch_preimage_mismatch():
    root = repo()
    digest = host.op_inspect(root, {})["tree_digest"]
    expect("preimage_mismatch", lambda: apply_patch(root, {
        "base_tree_digest": digest,
        "changes": [{"path": "pkg/calc.py", "preimage_sha256": "a" * 64,
                     "content_b64": base64.b64encode(b"x").decode()}]}, "jobreuse2"))


def test_patch_create_update_delete():
    root = repo()
    digest = host.op_inspect(root, {})["tree_digest"]
    old_sha = sha(os.path.join(root, "pkg", "calc.py"))
    new_body = b"def add(a, b):\n    if not isinstance(a, int):\n        raise ValueError\n    return a + b\n"
    args = {
        "base_tree_digest": digest,
        "changes": [
            {"path": "pkg/calc.py", "preimage_sha256": old_sha,
             "content_b64": base64.b64encode(new_body).decode()},
            {"path": "pkg/test_calc.py", "preimage_sha256": None,
             "content_b64": base64.b64encode(b"def test_x():\n    assert True\n").decode()},
        ],
    }
    result = apply_patch(root, args, "jobpatch1")
    assert result["tree_digest"] != digest
    actions = {item["path"]: item["action"] for item in result["applied"]}
    assert actions["pkg/calc.py"] == "update", actions
    assert actions["pkg/test_calc.py"] == "create", actions
    assert os.path.isfile(os.path.join(result["backup_dir"], "pkg", "calc.py")), "нет бэкапа preimage"
    with open(os.path.join(root, "pkg", "calc.py"), "rb") as fh:
        assert fh.read() == new_body
    replay = apply_patch(root, args, "jobpatch1")
    assert replay.get("replayed") is True, replay
    assert len(replay["applied"]) == 2
    assert sha(os.path.join(root, "pkg", "calc.py")) == hashlib.sha256(new_body).hexdigest()


def test_patch_delete():
    root = repo()
    target = os.path.join(root, "README.md")
    digest = host.op_inspect(root, {})["tree_digest"]
    result = apply_patch(root, {
        "base_tree_digest": digest,
        "changes": [{"path": "README.md", "preimage_sha256": sha(target),
                     "content_b64": None}]}, "jobdel1")
    assert result["applied"][0]["action"] == "delete"
    assert not os.path.exists(target)


def test_strong_tree_hash_detects_same_size_same_mtime_change():
    root = repo(with_git=False)
    path = os.path.join(root, "README.md")
    before = host.tree_digest(root)
    original = os.stat(path)
    with open(path, "wb") as fh:
        fh.write(b"# DEMO\n")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert os.path.getsize(path) == original.st_size
    assert host.tree_digest(root) != before


def test_symlink_blocks_snapshot_and_write():
    root = repo(with_git=False)
    original_digest = host.tree_digest(root)
    outside = tempfile.mkdtemp(prefix="wsops-outside-")
    DIRS.append(outside)
    with open(os.path.join(outside, "secret.txt"), "wb") as fh:
        fh.write(b"outside")
    link = os.path.join(root, "linked.txt")
    try:
        os.symlink(os.path.join(outside, "secret.txt"), link)
    except (OSError, NotImplementedError):
        return  # Windows without symlink privilege; other path tests still run.
    expect("symlink_forbidden", lambda: host.tree_digest(root))
    digest = hashlib.sha256(b"outside").hexdigest()
    expect("symlink_forbidden", lambda: apply_patch(root, {
        "base_tree_digest": original_digest,
        "changes": [{"path": "linked.txt", "preimage_sha256": digest,
                     "content_b64": base64.b64encode(b"replace").decode()}]}, "jobsym1"))


def test_partial_patch_journal_blocks_replay_and_new_job():
    from unittest import mock
    root = repo(with_git=False)
    before = host.tree_digest(root)
    a = os.path.join(root, "a.txt")
    b = os.path.join(root, "b.txt")
    args = {"base_tree_digest": before, "changes": [
        {"path": "a.txt", "preimage_sha256": None,
         "content_b64": base64.b64encode(b"after-a").decode()},
        {"path": "b.txt", "preimage_sha256": None,
         "content_b64": base64.b64encode(b"after-b").decode()},
    ]}
    real_replace = os.replace
    calls = {"n": 0}

    def fail_second(src, dst):
        if ".aihub-" in src:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected replace failure")
        return real_replace(src, dst)

    with mock.patch.object(host.os, "replace", side_effect=fail_second):
        expect("effect_uncertain", lambda: apply_patch(root, args, "jobpartial1"))
    assert open(a, "rb").read() == b"after-a"
    assert not os.path.exists(b)
    expect("checkout_requires_reconciliation", lambda: apply_patch(root, args, "jobpartial1"))
    expect("checkout_requires_reconciliation", lambda: apply_patch(root, {
        "base_tree_digest": host.tree_digest(root),
        "changes": [{"path": "c.txt", "preimage_sha256": None,
                     "content_b64": base64.b64encode(b"new").decode()}]}, "jobpartial2"))


def test_patch_rechecks_external_preimage_before_effect():
    root = repo(with_git=False)
    target = os.path.join(root, "README.md")
    original = sha(target)
    args = {"base_tree_digest": host.tree_digest(root), "changes": [{
        "path": "README.md", "preimage_sha256": original,
        "content_b64": base64.b64encode(b"agent edit\n").decode()}]}
    checks = {"n": 0}

    def fence_and_external_edit():
        checks["n"] += 1
        if checks["n"] == 2:
            with open(target, "wb") as fh:
                fh.write(b"user edit\n")
        return True

    try:
        host.op_apply_patch(root, args, "jobrace1", "jobrace1-attempt", 1,
                            fence_and_external_edit)
    except host.OpFail as exc:
        assert exc.code == "effect_uncertain", exc.code
    else:
        raise AssertionError("expected write to fail after external preimage change")
    assert open(target, "rb").read() == b"user edit\n"
    expect("checkout_requires_reconciliation", lambda: apply_patch(root, {
        "base_tree_digest": host.tree_digest(root),
        "changes": [{"path": "other.txt", "preimage_sha256": None,
                     "content_b64": base64.b64encode(b"x").decode()}]}, "jobrace2"))


def test_interprocess_writer_lock():
    root = repo(with_git=False)
    ready = os.path.join(root, "ready")
    code = (
        "import os,sys,time; sys.path.insert(0,sys.argv[1]); import workspace_host as h; "
        "root=sys.argv[2]; ready=sys.argv[3]; "
        "ctx=h._writer_lock(root); ctx.__enter__(); open(ready,'w').close(); time.sleep(1); ctx.__exit__(None,None,None)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, os.path.dirname(__file__), root, ready],
                            env=os.environ.copy())
    try:
        deadline = time.time() + 5
        while not os.path.exists(ready) and time.time() < deadline:
            time.sleep(0.02)
        assert os.path.exists(ready), "lock holder did not start"
        expect("writer_busy", lambda: _take_short_lock(root))
    finally:
        proc.wait(timeout=5)


def _take_short_lock(root):
    with host._writer_lock(root, timeout=0.1):
        pass


def test_run_checks_failclosed_without_isolated_runner():
    root = repo()
    snap = host.op_snapshot(root, {"label": "checks"}, "jobsnap2")
    plan = {"checks": [{"name": "ok", "cmd": [sys.executable, "-c", "print('1')"], "timeout_s": 60}]}
    os.environ.pop("AIHUB_ISOLATED_RUNNER_ARGV", None)
    expect("runner_unavailable", lambda: host.op_run_checks(
        root, {"snapshot_id": snap["snapshot_id"], "plan": plan}))
    assert os.path.isfile(os.path.join(root, "pkg", "calc.py")), "проверки не должны менять корень"


def test_run_checks_unknown_snapshot():
    root = repo()
    expect("unknown_snapshot", lambda: host.op_run_checks(
        root, {"snapshot_id": "nope", "plan": {"checks": [{"name": "x", "cmd": ["echo"]}]}}))


def test_package_bundle():
    root = repo()
    snap = host.op_snapshot(root, {"label": "bundle"}, "jobsnap3")
    out = host.op_package(root, {"snapshot_id": snap["snapshot_id"], "label": "bundle"})
    assert os.path.isfile(out["bundle"]["path"]), out
    with zipfile.ZipFile(out["bundle"]["path"]) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        assert "pkg/calc.py" in names
        assert ".env" not in names
    assert len(out["bundle"]["manifest_digest"]) == 64


def test_unknown_op_rejected():
    root = repo()
    payload = host.run_job({"id": "j1", "attempt_id": "a1", "prompt": json.dumps(
        {"kind": "workspace", "op": "shell", "args": {"root": root}})})
    assert payload["ok"] is False
    assert "не поддерживается" in payload["error"]


def test_root_missing_blocked():
    root = repo()
    payload = host.run_job({"id": "j2", "attempt_id": "a2", "prompt": json.dumps(
        {"kind": "workspace", "op": "inspect",
         "args": {"root": os.path.join(root, "no-such-dir")}})}, fence_check=lambda: True)
    assert payload["ok"] is False
    assert payload["error"].startswith("path_unavailable"), payload["error"]
    assert "blocked" in payload["error"]


def main() -> int:
    tests = [(name[5:], fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for label, fn in tests:
        check(label, fn)
    for name in PASSED:
        print(f"ok   {name}")
    for name in FAILED:
        print(f"FAIL {name}")
    print(f"\nвлазит {len(PASSED)}/{len(tests)}")
    print("workspace dir:", host.state_root())
    for path in DIRS:
        shutil.rmtree(path, ignore_errors=True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
