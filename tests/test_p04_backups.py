"""P-04: точка восстановления и журнал бэкапов.

Проверяется: zip собран из зафиксированного коммита, контрольная сумма
совпадает с файлом, строка появляется в ops/BACKUPS.md один раз.
"""
import hashlib
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.laptop.workspace_host as host  # noqa: E402


def _repo(root):
    subprocess.run(["git", "init", "-q", root], check=True, capture_output=True)
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("# demo\n")
    subprocess.run(["git", "-C", root, "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "init"], check=True, capture_output=True)
    return subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_backup_from_head_and_journal():
    with tempfile.TemporaryDirectory() as root:
        head = _repo(os.path.join(root, "repo"))
        os.makedirs(os.path.join(root, "ops"), exist_ok=True)
        with open(os.path.join(root, "ops", "BACKUPS.md"), "w", encoding="utf-8") as fh:
            fh.write("# Точки восстановления\n\n| Задача | Дата | Версия |\n| --- | --- | --- |\n")
        first = host.op_create_backup(root, {"label": "run-1", "host": "laptop"})
        assert first["head_sha"] == head
        assert first["path"] == f"ops/backups/run-1-{head[:8]}.zip"
        target = os.path.join(root, *first["path"].split("/"))
        assert os.path.isfile(target)
        assert hashlib.sha256(open(target, "rb").read()).hexdigest() == first["sha256"]
        assert first["journal_row_added"] is True
        second = host.op_create_backup(root, {"label": "run-1", "host": "laptop"})
        assert second["path"] == first["path"]
        assert second["sha256"] == first["sha256"]
        assert second["journal_row_added"] is False
        text = open(os.path.join(root, "ops", "BACKUPS.md"), encoding="utf-8").read()
        assert text.count(first["path"]) == 1


def test_backup_requires_git():
    with tempfile.TemporaryDirectory() as root:
        try:
            host.op_create_backup(root, {"label": "x"})
        except host.OpFail as exc:
            assert exc.code == "git_failed"
        else:
            raise AssertionError("бэкап без репозитория должен отклоняться")


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
