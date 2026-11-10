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
    expect("tree_changed", lambda: host.op_apply_patch(root, {
        "base_tree_digest": "0" * 64,
        "changes": [{"path": "pkg/new.py", "preimage_sha256": None, "content_b64": "eA=="}]},
        "jobwrongbase"))


def test_patch_requires_preimage_for_existing():
    root = repo()
    digest = host.op_inspect(root, {})["tree_digest"]
    expect("preimage_missing", lambda: host.op_apply_patch(root, {
        "base_tree_digest": digest,
        "changes": [{"path": "pkg/calc.py", "preimage_sha256": None,
                     "content_b64": base64.b64encode(b"x").decode()}]}, "jobreuse1"))


def test_patch_preimage_mismatch():
    root = repo()
    digest = host.op_inspect(root, {})["tree_digest"]
    expect("preimage_mismatch", lambda: host.op_apply_patch(root, {
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
    result = host.op_apply_patch(root, args, "jobpatch1")
    assert result["tree_digest"] != digest
    actions = {item["path"]: item["action"] for item in result["applied"]}
    assert actions["pkg/calc.py"] == "update", actions
    assert actions["pkg/test_calc.py"] == "create", actions
    assert os.path.isfile(os.path.join(result["backup_dir"], "pkg", "calc.py")), "нет бэкапа preimage"
    with open(os.path.join(root, "pkg", "calc.py"), "rb") as fh:
        assert fh.read() == new_body
    replay = host.op_apply_patch(root, args, "jobpatch1")
    assert replay.get("replayed") is True, replay
    assert len(replay["applied"]) == 2
    assert sha(os.path.join(root, "pkg", "calc.py")) == hashlib.sha256(new_body).hexdigest()


def test_patch_delete():
    root = repo()
    target = os.path.join(root, "README.md")
    digest = host.op_inspect(root, {})["tree_digest"]
    result = host.op_apply_patch(root, {
        "base_tree_digest": digest,
        "changes": [{"path": "README.md", "preimage_sha256": sha(target),
                     "content_b64": None}]}, "jobdel1")
    assert result["applied"][0]["action"] == "delete"
    assert not os.path.exists(target)


def test_run_checks_in_snapshot():
    root = repo()
    snap = host.op_snapshot(root, {"label": "checks"}, "jobsnap2")
    plan = {"checks": [
        {"name": "ok", "cmd": [sys.executable, "-c", "print('1')"], "timeout_s": 60},
        {"name": "bad", "cmd": [sys.executable, "-c", "raise SystemExit(3)"], "timeout_s": 60},
    ]}
    out = host.op_run_checks(root, {"snapshot_id": snap["snapshot_id"], "plan": plan})
    names = {item["name"]: item for item in out["checks"]}
    assert names["ok"]["exit_code"] == 0 and names["ok"]["passed"]
    assert names["bad"]["exit_code"] == 3 and not names["bad"]["passed"]
    assert out["ok"] is False
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
         "args": {"root": os.path.join(root, "no-such-dir")}})})
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
