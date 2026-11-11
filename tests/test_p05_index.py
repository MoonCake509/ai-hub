"""P-05: карта проекта по зафиксированному коммиту.

Проверяется: карта строится по HEAD, повтор не меняет файл, новый коммит
меняет карту.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.laptop.workspace_host as host  # noqa: E402


def _commit(root, name, text):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", name], check=True, capture_output=True)
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_index_tracks_commit():
    with tempfile.TemporaryDirectory() as root:
        repo = os.path.join(root, "repo")
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True)
        head = _commit(repo, "README.md", "# demo\n")
        first = host.op_refresh_index(root, {})
        assert first["head_sha"] == head
        assert first["files"] == 1
        assert first["changed"] is True
        index_path = os.path.join(root, "ops", "INDEX.md")
        text = open(index_path, encoding="utf-8").read()
        assert "`README.md`" in text and head in text
        second = host.op_refresh_index(root, {})
        assert second["changed"] is False and second["digest"] == first["digest"]
        new_head = _commit(repo, "docs/NOTES.md", "заметки\n")
        third = host.op_refresh_index(root, {})
        assert third["head_sha"] == new_head
        assert third["files"] == 2
        assert third["changed"] is True
        assert "`docs/NOTES.md`" in open(index_path, encoding="utf-8").read()


def test_index_requires_git():
    with tempfile.TemporaryDirectory() as root:
        try:
            host.op_refresh_index(root, {})
        except host.OpFail as exc:
            assert exc.code == "git_failed"
        else:
            raise AssertionError("карта без репозитория должна отклоняться")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failed += 1; print("FAIL", name, "-", exc)
    print(f"{2 - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
