"""CORE-02b: схема операций рабочего пространства (юнит, без БД и без устройства)."""
import sys

sys.path.insert(0, "/app")

from app import workspace as ws  # noqa: E402

PASSED = []
FAILED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"{name}: {type(exc).__name__}: {exc}")


def expect_fail(code, fn):
    try:
        fn()
    except ws.OpError as exc:
        assert exc.code == code, f"ожидался {code}, получен {exc.code}: {exc}"
        return
    raise AssertionError(f"ожидался отказ {code}")


def test_canon_relative():
    expect_fail("bad_path", lambda: ws.canon("relative/path"))


def test_canon_dotdot():
    expect_fail("traversal", lambda: ws.canon(r"..\outside"))


def test_canon_windows_ok():
    assert ws.canon(r"R:\AI-Hub\sub\\") == r"R:\AI-Hub\sub"


def test_resolve_dotdot():
    expect_fail("traversal", lambda: ws.resolve(r"R:\AI-Hub", "../other/file.txt"))


def test_resolve_absolute():
    expect_fail("absolute_path", lambda: ws.resolve(r"R:\AI-Hub", r"C:\Windows\win.ini"))


def test_resolve_outside_via_join():
    expect_fail("traversal", lambda: ws.resolve(r"R:\AI-Hub", "sub/../../x"))


def test_resolve_inside_ok():
    assert ws.resolve(r"R:\AI-Hub", "hub/app/main.py") == r"R:\AI-Hub\hub\app\main.py"


def test_within_case_insensitive():
    assert ws.is_within(r"R:\AI-Hub", r"r:\ai-hub\hub\app\main.py")


def test_prefix_trap():
    assert not ws.is_within(r"C:\AI-Hub", r"C:\AI-Hub-stand\hub\app.py")


def test_deny_env():
    assert ws.denied_reason("project/.env") == "файл окружения"
    assert ws.denied_reason(".env.local") == "файл окружения"


def test_deny_secret():
    assert ws.denied_reason("keys/server.pem") == "секрет/дамп"
    assert ws.denied_reason("id_rsa") == "секрет"


def test_deny_dir():
    assert "node_modules" in (ws.denied_reason("web/node_modules/x/y.js") or "")


def test_unknown_op():
    expect_fail("unknown_op", lambda: ws.validate("rm_rf", {"root": r"R:\AI-Hub"}))


def test_extra_field():
    expect_fail("bad_arg", lambda: ws.validate("inspect", {"root": r"R:\AI-Hub", "cmd": "dir"}))


def test_list_limit():
    expect_fail("bad_arg", lambda: ws.validate(
        "list_files", {"root": r"R:\AI-Hub", "limit": ws.MAX_LIST + 1}))


def test_list_glob_escape():
    expect_fail("bad_arg", lambda: ws.validate(
        "list_files", {"root": r"R:\AI-Hub", "glob": "../*"}))


def test_read_files_limit():
    files = [{"path": f"f{i}.py"} for i in range(ws.MAX_READ_FILES + 1)]
    expect_fail("bad_arg", lambda: ws.validate("read_files", {"root": r"R:\AI-Hub", "files": files}))


def test_read_denied_secret():
    expect_fail("denied_path", lambda: ws.validate(
        "read_files", {"root": r"R:\AI-Hub", "files": [{"path": ".env"}]}))


def test_patch_needs_base():
    expect_fail("bad_arg", lambda: ws.validate("apply_patch", {
        "root": r"R:\AI-Hub", "changes": [{"path": "a.py", "content_b64": "eA=="}]}))


def test_patch_bad_base64():
    expect_fail("bad_arg", lambda: ws.validate("apply_patch", {
        "root": r"R:\AI-Hub", "base_tree_digest": "0" * 64,
        "changes": [{"path": "a.py", "content_b64": "not base64!!"}]}))


def test_patch_needs_preimage_or_content():
    expect_fail("bad_arg", lambda: ws.validate("apply_patch", {
        "root": r"R:\AI-Hub", "base_tree_digest": "0" * 64,
        "changes": [{"path": "a.py"}]}))


def test_patch_ok_normalized():
    out = ws.validate("apply_patch", {
        "root": r"R:\AI-Hub", "base_tree_digest": "a" * 64,
        "changes": [{"path": "hub/app/x.py", "content_b64": "eA=="}]})
    assert out["changes"][0]["path"] == "hub/app/x.py"


def test_patch_denied():
    expect_fail("denied_path", lambda: ws.validate("apply_patch", {
        "root": r"R:\AI-Hub", "base_tree_digest": "a" * 64,
        "changes": [{"path": ".env", "content_b64": "eA=="}]}))


def test_checks_need_snapshot():
    expect_fail("bad_arg", lambda: ws.validate(
        "run_checks", {"root": r"R:\AI-Hub", "checks": ["unit"]}))


def test_package_need_snapshot():
    expect_fail("bad_arg", lambda: ws.validate("package", {"root": r"R:\AI-Hub"}))


def test_op_set_matches_plan():
    assert set(ws.OPS) == {"inspect", "snapshot", "list_files", "read_files",
                           "apply_patch", "run_checks", "package"}
    assert ws.WRITES_ROOT == {"apply_patch"}


TESTS = [(name[5:], fn) for name, fn in sorted(globals().items())
         if name.startswith("test_") and callable(fn)]
for label, fn in TESTS:
    check(label, fn)

for name in PASSED:
    print(f"ok   {name}")
for name in FAILED:
    print(f"FAIL {name}")
print(f"\nвлазит {len(PASSED)}/{len(TESTS)}")
raise SystemExit(1 if FAILED else 0)
