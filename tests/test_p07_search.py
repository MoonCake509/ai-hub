"""P-07: архивариус и поиск — карта без секретов, поиск и фрагмент с версией.

Проверяется на настоящем Git-репозитории: карта строится по коммиту, секретные
файлы исключаются, поиск возвращает путь и версию, фрагмент сверяется с SHA.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.laptop.workspace_host as host  # noqa: E402


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "notes.md").write_text("Сложение двух чисел\n", encoding="utf-8")
    (repo / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
    (repo / "server.pem").write_text("-----BEGIN-----\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    return repo


def test_index_skips_secrets_and_is_idempotent(tmp_path):
    repo = _repo(tmp_path)
    root = str(tmp_path)
    first = host.op_refresh_index(root, {})
    assert first["files"] == 2
    assert sorted(first["secrets"]) == [".env", "server.pem"]
    text = (tmp_path / "ops" / "INDEX.md").read_text(encoding="utf-8")
    assert "app.py" in text and ".env" not in text.split("Исключены")[0]
    assert "Версия (git)" in text
    second = host.op_refresh_index(root, {})
    assert second["changed"] is False and second["digest"] == first["digest"]


def test_search_returns_path_line_and_sha(tmp_path):
    _repo(tmp_path)
    result = host.op_search_git(str(tmp_path), {"q": "add", "limit": 10})
    assert result["head_sha"]
    assert any(m["path"] == "app.py" and m["kind"] == "content" for m in result["matches"])
    assert all(m["sha256"] != "—" for m in result["matches"])
    assert all(".env" not in m["path"] for m in result["matches"])


def test_read_at_verifies_version(tmp_path):
    _repo(tmp_path)
    index = host.op_refresh_index(str(tmp_path), {})
    first = host.op_read_at(str(tmp_path), {"path": "app.py"})
    digest = first["sha256"][:16]
    got = host.op_read_at(str(tmp_path), {"path": "app.py", "sha256": digest})
    assert "def add" in got["text"] and got["sha256"].startswith(digest)
    try:
        host.op_read_at(str(tmp_path), {"path": "app.py", "sha256": "0" * 16})
    except host.OpFail as exc:
        assert exc.code == "sha_mismatch"
    else:
        raise AssertionError("чужой SHA принят")
    assert index["files"] == 2
