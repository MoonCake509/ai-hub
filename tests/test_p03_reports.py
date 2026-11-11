"""P-03: живые отчёты проекта — журнал TASKFLOW и черновики PR.

Проверяется: сборка из данных прогона, запись только в ops/, повтор не меняет
файл, путь вне ops/ отклоняется.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hub.app import reports  # noqa: E402
import agents.laptop.workspace_host as host  # noqa: E402

PROJECT = {"name": "Demo", "branch": "main", "head_sha": "a" * 40, "workdir": ""}
RUN = {
    "id": "run-1",
    "project_id": "p-1",
    "state": "active",
    "template_id": "five-roles",
    "branch": "task/demo",
    "base_sha": "a" * 40,
    "candidate_sha": "b" * 40,
    "tasks": [
        {"key": "plan", "title": "План", "role": "planner", "status": "done",
         "candidate_sha": None, "receipt": {}},
        {"key": "code", "title": "Правка", "role": "coder", "status": "running",
         "candidate_sha": "b" * 40, "receipt": {}},
        {"key": "test", "title": "Проверка", "role": "tester", "status": "queued",
         "receipt": {"checks": [{"name": "pytest", "ok": True, "exit_code": 0}]}},
    ],
}


def test_taskflow_has_facts():
    text = reports.build_taskflow(PROJECT, RUN)
    assert "TASKFLOW — Demo" in text
    assert "run-1" in text
    assert "| Правка | coder | выполняется | bbbbbbbbbbbb | — |" in text
    assert "| Проверка | tester | в очереди | — | 1/1 |" in text
    assert "Шаг «Правка», роль coder" in text


def test_pr_draft_has_base_candidate_checks():
    drafts = reports.build_pr_drafts(PROJECT, RUN)
    assert sorted(drafts) == ["ops/pr/code.md", "ops/pr/plan.md", "ops/pr/test.md"]
    body = drafts["ops/pr/test.md"]
    assert "aaaaaaaaaaaa" in body and "bbbbbbbbbbbb" in body.replace("b" * 12, "b" * 12)
    assert "| pytest | ok | 0 |" in body


def test_write_reports_ops_only_and_idempotent():
    with tempfile.TemporaryDirectory() as root:
        args = {"reports": [{"path": "ops/TASKFLOW.md", "content": "# журнал\n"},
                            {"path": "ops/pr/code.md", "content": "# PR\n"}]}
        first = host.op_write_reports(root, args)
        assert sorted(first["written"]) == ["ops/TASKFLOW.md", "ops/pr/code.md"]
        assert os.path.isfile(os.path.join(root, "ops", "TASKFLOW.md"))
        second = host.op_write_reports(root, args)
        assert second["written"] == []
        assert sorted(second["skipped"]) == ["ops/TASKFLOW.md", "ops/pr/code.md"]


def test_write_reports_rejects_outside_ops():
    with tempfile.TemporaryDirectory() as root:
        for path in ("repo/README.md", "ops/../repo/x.md", "C:/x.md"):
            try:
                host.op_write_reports(root, {"reports": [{"path": path, "content": "x"}]})
            except host.OpFail as exc:
                assert exc.code in {"denied_path", "bad_request", "bad_path"}
            else:
                raise AssertionError(f"принят путь вне ops/: {path}")
        assert not os.path.exists(os.path.join(root, "repo"))


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("FAIL", name, "-", exc)
    print(f"{4 - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)