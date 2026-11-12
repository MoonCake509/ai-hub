"""P-08: отдельная рабочая копия задачи, сверка ветки и слияние проверенного SHA."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents" / "laptop"))

import workspace_host as wh  # noqa: E402


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    (root / "ops").mkdir()
    (root / "ops" / "TASKFLOW.md").write_text("# журнал\n", encoding="utf-8")
    (root / "ops" / "INDEX.md").write_text("# карта\n", encoding="utf-8")
    (root / "ops" / "pr").mkdir()
    (root / "ops" / "pr" / "plan.md").write_text("# план\n", encoding="utf-8")
    return root


def test_task_start_pins_base_and_copies_ops(tmp_path):
    root = _project(tmp_path)
    base = _git(root / "repo", "rev-parse", "HEAD")
    res = wh.op_task_start(str(root), {"run_id": "a" * 32, "branch": "task/aaaaaaaa"})
    assert res["base_sha"] == base
    assert res["head_sha"] == base
    assert Path(res["task_root"], "repo", "app.py").is_file()
    assert Path(res["task_root"], "ops", "TASKFLOW.md").is_file()
    # основная копия не тронута: ветка прежняя
    assert _git(root / "repo", "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # повтор не создаёт вторую копию
    again = wh.op_task_start(str(root), {"run_id": "a" * 32, "branch": "task/aaaaaaaa"})
    assert again["created"] is False


def test_verify_stops_on_wrong_branch(tmp_path):
    root = _project(tmp_path)
    res = wh.op_task_start(str(root), {"run_id": "b" * 32, "branch": "task/bbbbbbbb"})
    ok = wh.op_verify_task(res["task_root"], {"branch": "task/bbbbbbbb"})
    assert ok["branch"] == "task/bbbbbbbb"
    try:
        wh.op_verify_task(res["task_root"], {"branch": "task/other"})
    except wh.OpFail as exc:
        assert exc.code == "task_mismatch"
    else:
        raise AssertionError("несовпадение ветки не остановило шаг")


def test_merge_ff_only_and_base_moved(tmp_path):
    root = _project(tmp_path)
    res = wh.op_task_start(str(root), {"run_id": "c" * 32, "branch": "task/cccccccc"})
    task_repo = Path(res["task_root"], "repo")
    (task_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")
    _git(task_repo, "add", ".")
    _git(task_repo, "commit", "-q", "-m", "fix")
    sha = _git(task_repo, "rev-parse", "HEAD")

    merged = wh.op_task_merge(str(res["task_root"]), {"branch": "task/cccccccc", "sha": sha})
    assert merged["merged"] is True and merged["sha"] == sha
    assert _git(root / "repo", "rev-parse", "HEAD") == sha

    # ветка задачи сохранена после слияния
    assert _git(root / "repo", "rev-parse", "task/cccccccc") == sha

    # сдвиг основной ветки: слияние не выполняется, ветка задачи остаётся
    other = wh.op_task_start(str(root), {"run_id": "d" * 32, "branch": "task/dddddddd"})
    other_repo = Path(other["task_root"], "repo")
    (other_repo / "b.py").write_text("x = 1\n", encoding="utf-8")
    _git(other_repo, "add", ".")
    _git(other_repo, "commit", "-q", "-m", "other")
    other_sha = _git(other_repo, "rev-parse", "HEAD")
    (root / "repo" / "app.py").write_text("# сдвиг\n", encoding="utf-8")
    _git(root / "repo", "add", ".")
    _git(root / "repo", "commit", "-q", "-m", "move main")

    blocked = wh.op_task_merge(str(other["task_root"]),
                               {"branch": "task/dddddddd", "sha": other_sha})
    assert blocked["merged"] is False and blocked["reason"] == "base_moved"
    assert _git(root / "repo", "rev-parse", "task/dddddddd") == other_sha


def test_archive_registers_artifacts_and_setup_runs(tmp_path):
    root = _project(tmp_path)
    res = wh.op_task_start(str(root), {"run_id": "e" * 32, "branch": "task/eeeeeeee"})
    setup = wh.op_setup_env(res["task_root"], {"cmd": [sys.executable, "-c", "print('ok')"]})
    assert setup["ok"] is True

    archived = wh.op_archive_artifacts(str(root), {"run_id": "e" * 32, "sha": "deadbeef",
                                                   "branch": "task/eeeeeeee"})
    assert archived["artifacts"] >= 2
    text = (root / "ops" / "ARTIFACTS.md").read_text(encoding="utf-8")
    assert "ops/pr/plan.md" in text and "eeeeeeee" in text
    # повтор не дублирует раздел запуска
    wh.op_archive_artifacts(str(root), {"run_id": "e" * 32, "sha": "deadbeef"})
    assert (root / "ops" / "ARTIFACTS.md").read_text(encoding="utf-8").count("## eeeeeeee") == 1


def test_publish_requires_remote(tmp_path):
    root = _project(tmp_path)
    try:
        wh.op_publish_branch(str(root), {"branch": "main", "remote": "origin"})
    except wh.OpFail as exc:
        assert exc.code == "no_remote"
    else:
        raise AssertionError("публикация без remote не отклонена")
