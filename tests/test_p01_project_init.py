"""P-01: создание рабочей папки проекта операцией host worker.

Проверяется на настоящем временном каталоге: шаблон, git init, первый коммит,
идемпотентный повтор и запрет выхода за пределы корня.
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agents", "laptop"))
import workspace_host as host  # noqa: E402

PASSED, FAILED = [], []
BASE = tempfile.mkdtemp(prefix="p01-")


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


def git(root, *args):
    proc = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


ARGS = {
    "name": "Demo",
    "branch": "main",
    "author_name": "AI Hub",
    "author_email": "aihub@local",
    "template_version": "1",
    "template_files": {
        "README.md": "# Demo\n",
        "docs/NOTES.md": "заметки\n",
        ".gitignore": ".env\n",
    },
}


def test_creates_tree_and_commit():
    root = os.path.join(BASE, "new-project-0001")
    result = host.op_init_project(root, dict(ARGS))
    assert result["created_dir"] is True
    assert result["committed"] is True
    assert sorted(result["written"]) == [".gitignore", "README.md", "docs/NOTES.md"]
    assert os.path.isfile(os.path.join(root, "docs", "NOTES.md"))
    assert result["branch"] == "main"
    code, head = git(root, "rev-parse", "HEAD")
    assert code == 0 and head == result["head_sha"]
    code, log = git(root, "log", "--oneline")
    assert len(log.splitlines()) == 1, log


def test_repeat_keeps_user_files_and_commit():
    root = os.path.join(BASE, "repeat-project-0002")
    first = host.op_init_project(root, dict(ARGS))
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# изменено пользователем\n")
    second = host.op_init_project(root, dict(ARGS))
    assert second["written"] == []
    assert sorted(second["skipped"]) == [".gitignore", "README.md", "docs/NOTES.md"]
    assert second["committed"] is False
    assert second["head_sha"] == first["head_sha"]
    with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
        assert fh.read().strip() == "# изменено пользователем"


def test_rejects_path_escape():
    root = os.path.join(BASE, "escape-project-0003")
    bad = dict(ARGS)
    bad["template_files"] = {"../outside.txt": "x"}
    expect("bad_path", lambda: host.op_init_project(root, bad))
    assert not os.path.exists(os.path.join(BASE, "outside.txt"))


def test_rejects_absolute_path():
    root = os.path.join(BASE, "abs-project-0004")
    bad = dict(ARGS)
    bad["template_files"] = {"/etc/passwd": "x"}
    expect("bad_path", lambda: host.op_init_project(root, bad))


if __name__ == "__main__":
    check("creates_tree_and_commit", test_creates_tree_and_commit)
    check("repeat_keeps_user_files_and_commit", test_repeat_keeps_user_files_and_commit)
    check("rejects_path_escape", test_rejects_path_escape)
    check("rejects_absolute_path", test_rejects_absolute_path)
    shutil.rmtree(BASE, ignore_errors=True)
    for name in PASSED:
        print("PASS", name)
    for err in FAILED:
        print("FAIL", err)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    sys.exit(1 if FAILED else 0)