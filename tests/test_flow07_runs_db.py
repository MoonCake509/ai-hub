"""FLOW-P1: DB/integration тесты контроллера запусков — ТОЛЬКО изолированная БД.

Запуск внутри тестового контейнера (aihub-hub-test, БД aihub_test):
    docker exec -i aihub-hub-test python -u -X utf8 - < tests/test_flow07_runs_db.py

НЕ запускать против production: тест создаёт собственные проекты/запуски в той
БД, к которой подключён контейнер. Workspace-операции хоста подменяются
scripted fake'ами — ни одна управляемая операция на реальный хост не уходит.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
import uuid

sys.path.insert(0, "/app")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import db, flow_gates, packets, runs, templates, workspace  # noqa: E402

CANDIDATE = "sha256:" + "c" * 64
ISOLATION = {
    "network": "none", "rootfs_read_only": True, "cap_drop_all": True,
    "no_new_privileges": True, "resource_limits": True,
    "snapshot_read_only": True, "nonroot": True, "ephemeral_workspace": True,
    "image": "sha256:" + "a" * 64,
}


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeWorkspace:
    """Scripted host: create_op_job регистрирует задание, job_result отдаёт сценарий."""

    def __init__(self):
        self.jobs = {}
        self.created = []

    def create_op_job(self, op, args, host_id, project_id=None, **kwargs):
        job_id = f"ws-{op}-{uuid.uuid4().hex[:8]}"
        self.jobs[job_id] = None  # статус появится через set_result
        self.created.append((op, args, job_id))
        return {"job": {"id": job_id}, "created": True}

    def set_result(self, job_id, result=None, status="succeeded", error=None):
        self.jobs[job_id] = {"status": status, "result": result, "error": error}

    def job_result(self, job_id):
        return self.jobs.get(job_id)

    def patch(self):
        self._orig_create = workspace.create_op_job
        self._orig_result = workspace.job_result
        workspace.create_op_job = self.create_op_job
        workspace.job_result = self.job_result

    def restore(self):
        workspace.create_op_job = self._orig_create
        workspace.job_result = self._orig_result


FAKE = FakeWorkspace()


def resolve_agent(agent_id):
    if not agent_id:
        return None, "агент не задан"
    return f"bridge-{agent_id}", None


def plain_template():
    """Шаблон без контекста — для тестов выдачи/идемпотентности/бюджетов."""
    steps = []
    for item in templates.CHAIN:
        role = templates.ROLES[item["role"]]
        steps.append({"key": item["key"], "role": item["role"], "agent": "codex",
                      "prompt": role["prompt"], "depends_on": list(item["depends_on"]),
                      "context": {}})
    return {"id": "five-roles", "version": 1, "steps": steps}


def make_project(name=None):
    return db.create_project(name or f"flow-{uuid.uuid4().hex[:8]}",
                             host_id="testhost", workdir="/tmp/flow-workdir")


def complete_job(job_id, output, ok=True):
    with db._lock:
        conn = db.connect()
        conn.run("begin")
        conn.run(db._qualify(
            "update aihub.jobs set status = :s, result_output = :o, finished_at = now()"
            " where id = :j"), s="succeeded" if ok else "failed", o=output, j=job_id)
        conn.run("commit")


def task_of(run_id, key):
    item = runs.get_run(run_id)
    return next(t for t in item["tasks"] if t["key"] == key)


PLAN_OUT = '{"tasks":[{"key":"k1","title":"сделать","files":["src/a.py"],"acceptance":"ok"}],"risks":[]}'
CODE_OUT = '{"summary":"готово","files":["src/a.py"],"notes":null}'
TEST_OUT = ('{"ok":true,"candidate_digest":"%s","checks":[{"name":"unit","cmd":["pytest"],"ok":true}]}' % CANDIDATE)
REVIEW_OUT = ('{"verdict":"approve","candidate_digest":"%s","findings":[]}' % CANDIDATE)
ARCHIVE_OUT = '{"summary":"итог","artifacts":[{"path":"docs/itog.md","kind":"doc"}],"decisions":[]}'


class DbTestCase(unittest.TestCase):
    def setUp(self):
        db.init()
        # Изоляция между тестами: старые активные запуски этого же тестового
        # прогона закрываем — иначе limit полler'а съедают они, а не наш запуск.
        with db._lock:
            conn = db.connect()
            conn.run(db._qualify(
                "update aihub.runs set state = 'cancelled', finished_at = now()"
                " where state in ('active', 'paused')"))

    def tearDown(self):
        FAKE.jobs.clear()
        FAKE.created.clear()


class CreateRunTest(DbTestCase):
    def test_goal_criteria_snapshots_stored(self):
        project = make_project()
        db.update_role(project["id"], "coder", agent_id="agent-coder",
                       profile="prof-a", model="model-m")
        run = runs.create_run(project["id"], plain_template(),
                              {"max_cycles": 2, "budgets": {"max_dispatches": 10}},
                              goal="сделать фичу", criteria=["тесты зелёные"],
                              request_key=f"rk-{uuid.uuid4().hex[:8]}")
        item = runs.get_run(run["run_id"])
        self.assertEqual(item["goal"], "сделать фичу")
        self.assertEqual(item["criteria"], ["тесты зелёные"])
        self.assertEqual(item["budgets"], {"max_dispatches": 10})
        self.assertEqual(item["assignments"]["code"]["agent_id"], "agent-coder")
        self.assertEqual(item["assignments"]["code"]["model"], "model-m")
        self.assertEqual(item["project_snapshot"]["project_workdir"], "/tmp/flow-workdir")
        for task in item["tasks"]:
            self.assertIn("context", task)
            self.assertEqual(task["assignment"]["role"], task["role"])

    def test_idempotent_replay_same_content(self):
        project = make_project()
        key = f"rk-{uuid.uuid4().hex[:8]}"
        first = runs.create_run(project["id"], plain_template(), {},
                                goal="цель", request_key=key)
        second = runs.create_run(project["id"], plain_template(), {},
                                 goal="цель", request_key=key)
        self.assertFalse(first.get("existing"))
        self.assertTrue(second.get("existing"))
        self.assertEqual(first["run_id"], second["run_id"])

    def test_same_key_different_content_conflict(self):
        project = make_project()
        key = f"rk-{uuid.uuid4().hex[:8]}"
        runs.create_run(project["id"], plain_template(), {}, goal="цель А", request_key=key)
        with self.assertRaises(runs.RunError) as ctx:
            runs.create_run(project["id"], plain_template(), {}, goal="цель Б", request_key=key)
        self.assertEqual(ctx.exception.code, "idempotency_conflict")

    def test_bad_budget_rejected(self):
        project = make_project()
        with self.assertRaises(runs.RunError) as ctx:
            runs.create_run(project["id"], plain_template(),
                            {"budgets": {"max_dispatches": -1}})
        self.assertEqual(ctx.exception.code, "bad_policy")

    def test_assignment_snapshot_immune_to_role_card_change(self):
        """Правка карточки роли после создания запуска назначение не меняет."""
        project = make_project()
        db.update_role(project["id"], "planner", agent_id="agent-before", model="m1")
        run = runs.create_run(project["id"], plain_template(), {})
        db.update_role(project["id"], "planner", agent_id="agent-after", model="m2")
        result = runs.dispatch(run["run_id"], resolve_agent)
        self.assertTrue(result["dispatched"], result)
        job = db.get_job(result["dispatched"][0]["job_id"])
        self.assertEqual(job["agent_id"], "bridge-agent-before")
        self.assertEqual(job["requested_model"], "m1")
        self.assertEqual(job["run_snapshot"]["project_workdir"], "/tmp/flow-workdir")
        self.assertEqual(job["run_snapshot"]["run_id"], run["run_id"])


class DispatchGateTest(DbTestCase):
    def test_context_failure_blocks_dispatch_and_run(self):
        """Нет готового контекста — задание не выдаётся, зависимые blocked, run failed."""
        project = make_project()
        template = templates.template("src/app.py", [])
        run = runs.create_run(project["id"], template, {})
        # Нет живого host worker: сборка контекста не ставится.
        result = runs.dispatch(run["run_id"], resolve_agent)
        self.assertEqual(result["dispatched"], [])
        item = runs.get_run(run["run_id"])
        plan = next(t for t in item["tasks"] if t["key"] == "plan")
        self.assertEqual(plan["state"], "failed")
        self.assertIn("контекст", plan["error"])
        for key in ("code", "test", "review", "archive"):
            self.assertEqual(next(t for t in item["tasks"] if t["key"] == key)["state"],
                             "blocked")
        self.assertEqual(item["state"], "failed")

    def test_role_gate_waits_for_candidate_and_receipt(self):
        """Tester не выдаётся, пока нет кандидата и receipt; событие ждёт вход."""
        project = make_project()
        run = runs.create_run(project["id"], plain_template(), {})
        first = runs.dispatch(run["run_id"], resolve_agent)
        self.assertEqual([d["task"] for d in first["dispatched"]], ["plan"])
        complete_job(first["dispatched"][0]["job_id"], PLAN_OUT)
        runs.on_task_result(first["dispatched"][0]["job_id"], True)
        second = runs.dispatch(run["run_id"], resolve_agent)
        self.assertEqual([d["task"] for d in second["dispatched"]], ["code"])
        complete_job(second["dispatched"][0]["job_id"], CODE_OUT)
        runs.on_task_result(second["dispatched"][0]["job_id"], True)
        # Кандидата и проверок нет: test ждёт, задание не создаётся.
        third = runs.dispatch(run["run_id"], resolve_agent)
        self.assertEqual(third["dispatched"], [])
        item = runs.get_run(run["run_id"])
        test_task = next(t for t in item["tasks"] if t["key"] == "test")
        self.assertEqual(test_task["state"], "pending")
        waits = [e for e in item["events"] if e["event"] == "task_waiting_input"]
        self.assertTrue(waits)


class CancelCasTest(DbTestCase):
    def test_cancel_cascades_to_queued_jobs(self):
        project = make_project()
        run = runs.create_run(project["id"], plain_template(), {})
        result = runs.dispatch(run["run_id"], resolve_agent)
        job_id = result["dispatched"][0]["job_id"]
        state = runs.set_state(run["run_id"], "cancel")
        self.assertEqual(state["state"], "cancelled")
        self.assertEqual(db.get_job(job_id)["status"], "cancelled")
        item = runs.get_run(run["run_id"])
        self.assertEqual(item["state"], "cancelled")
        self.assertIn(job_id, item["cancel_info"]["jobs_cancelled"])
        self.assertEqual(next(t for t in item["tasks"] if t["key"] == "plan")["state"],
                         "cancelled")
        # Повторная отмена — конфликт состояния.
        with self.assertRaises(runs.RunError):
            runs.set_state(run["run_id"], "cancel")
        # Выдача после отмены невозможна.
        late = runs.dispatch(run["run_id"], resolve_agent)
        self.assertEqual(late["dispatched"], [])

    def test_result_after_cancel_kept_not_advanced(self):
        project = make_project()
        run = runs.create_run(project["id"], plain_template(), {})
        result = runs.dispatch(run["run_id"], resolve_agent)
        job_id = result["dispatched"][0]["job_id"]
        runs.set_state(run["run_id"], "cancel")
        complete_job(job_id, PLAN_OUT)
        outcome = runs.on_task_result(job_id, True)
        self.assertEqual(outcome["state"], "cancelled")
        item = runs.get_run(run["run_id"])
        self.assertEqual(item["state"], "cancelled")
        self.assertTrue(any(e["event"] == "result_after_cancel" for e in item["events"]))

    def test_duplicate_result_is_cas_noop(self):
        project = make_project()
        run = runs.create_run(project["id"], plain_template(), {})
        result = runs.dispatch(run["run_id"], resolve_agent)
        job_id = result["dispatched"][0]["job_id"]
        complete_job(job_id, PLAN_OUT)
        first = runs.on_task_result(job_id, True)
        self.assertEqual(first["state"], "done")
        again = runs.on_task_result(job_id, True)
        self.assertTrue(again["duplicate"])
        item = runs.get_run(run["run_id"])
        plan = next(t for t in item["tasks"] if t["key"] == "plan")
        self.assertEqual(plan["state"], "done")
        self.assertEqual(sum(1 for e in item["events"] if e["event"] == "task_done"), 1)
        self.assertTrue(any(e["event"] == "duplicate_result" for e in item["events"]))


class BudgetTest(DbTestCase):
    def test_max_dispatches_fails_run(self):
        project = make_project()
        run = runs.create_run(project["id"], plain_template(),
                              {"budgets": {"max_dispatches": 1}})
        first = runs.dispatch(run["run_id"], resolve_agent)
        self.assertEqual(len(first["dispatched"]), 1)
        complete_job(first["dispatched"][0]["job_id"], PLAN_OUT)
        runs.on_task_result(first["dispatched"][0]["job_id"], True)
        second = runs.dispatch(run["run_id"], resolve_agent)
        self.assertEqual(second["state"], "failed")
        self.assertIn("бюджет", second["reason"])
        item = runs.get_run(run["run_id"])
        self.assertEqual(item["state"], "failed")
        self.assertTrue(any(e["event"] == "budget_exceeded" for e in item["events"]))

    def test_budget_used_counters_increment(self):
        project = make_project()
        run = runs.create_run(project["id"], plain_template(), {})
        result = runs.dispatch(run["run_id"], resolve_agent)
        complete_job(result["dispatched"][0]["job_id"], PLAN_OUT)
        runs.on_task_result(result["dispatched"][0]["job_id"], True)
        item = runs.get_run(run["run_id"])
        self.assertEqual(item["budget_used"]["dispatches"], 1)
        self.assertEqual(item["budget_used"]["job_calls"], 1)


def _green_receipt():
    return {
        "snapshot_id": "snap-cand", "ok": True,
        "checks": [{"name": "unit", "cmd": ["pytest", "-q"], "exit_code": 0,
                    "passed": True, "elapsed_s": 1.0, "stdout_tail": "", "stderr_tail": ""}],
        "isolation": ISOLATION, "at": "2026-11-11T00:00:00",
    }


class PipelineTest(DbTestCase):
    """Сквозной цикл: plan -> code -> candidate snapshot -> run_checks -> test -> review -> archive."""

    def setUp(self):
        super().setUp()
        FAKE.patch()
        db.update_project(make_project_id := make_project()["id"], settings={
            "checks": {"unit": {"cmd": ["pytest", "-q"], "timeout_s": 60}}})
        self.project_id = make_project_id

    def tearDown(self):
        FAKE.restore()
        super().tearDown()

    def _run_checks_green(self):
        """Все проверки зелёные: receipt принят, тестировщик выдаётся с receipt."""
        run = runs.create_run(self.project_id, plain_template(), {"max_cycles": 2})
        rid = run["run_id"]
        # plan
        r = runs.dispatch(rid, resolve_agent)
        complete_job(r["dispatched"][0]["job_id"], PLAN_OUT)
        runs.on_task_result(r["dispatched"][0]["job_id"], True)
        # code
        r = runs.dispatch(rid, resolve_agent)
        complete_job(r["dispatched"][0]["job_id"], CODE_OUT)
        runs.on_task_result(r["dispatched"][0]["job_id"], True)
        # poll: снимок кандидата
        runs.poll_active(resolve_agent)
        snap_jobs = [c for c in FAKE.created if c[0] == "snapshot"]
        self.assertEqual(len(snap_jobs), 1)
        FAKE.set_result(snap_jobs[0][2],
                        {"snapshot_id": "snap-cand", "tree_digest": CANDIDATE})
        runs.poll_active(resolve_agent)
        # poll: проверки
        check_jobs = [c for c in FAKE.created if c[0] == "run_checks"]
        self.assertEqual(len(check_jobs), 1)
        args = check_jobs[0][1]
        self.assertEqual(args["snapshot_id"], "snap-cand")
        FAKE.set_result(check_jobs[0][2], _green_receipt())
        runs.poll_active(resolve_agent)
        item = runs.get_run(rid)
        self.assertEqual(item["candidate_digest"], CANDIDATE)
        self.assertEqual(item["check_receipt"]["snapshot_id"], "snap-cand")
        # poll_active уже выдал test: задание создано с receipt и кандидатом в prompt.
        test_task = next(t for t in item["tasks"] if t["key"] == "test")
        self.assertEqual(test_task["state"], "dispatched")
        job = db.get_job(test_task["job_id"])
        self.assertIn("Receipt", job["prompt"])
        self.assertIn(CANDIDATE, job["prompt"])
        complete_job(test_task["job_id"], TEST_OUT)
        outcome = runs.on_task_result(test_task["job_id"], True)
        self.assertEqual(outcome["state"], "done")
        return rid

    def test_full_pipeline_green(self):
        rid = self._run_checks_green()
        # review
        r = runs.dispatch(rid, resolve_agent)
        self.assertEqual([d["task"] for d in r["dispatched"]], ["review"])
        job = db.get_job(r["dispatched"][0]["job_id"])
        self.assertIn(CANDIDATE, job["prompt"])
        complete_job(r["dispatched"][0]["job_id"], REVIEW_OUT)
        self.assertEqual(runs.on_task_result(r["dispatched"][0]["job_id"], True)["state"], "done")
        # archive
        r = runs.dispatch(rid, resolve_agent)
        self.assertEqual([d["task"] for d in r["dispatched"]], ["archive"])
        complete_job(r["dispatched"][0]["job_id"], ARCHIVE_OUT)
        self.assertEqual(runs.on_task_result(r["dispatched"][0]["job_id"], True)["state"], "done")
        item = runs.get_run(rid)
        self.assertEqual(item["state"], "done")
        self.assertTrue(any(e["event"] == "run_done" for e in item["events"]))

    def test_tester_wrong_candidate_digest_returns_to_coder(self):
        rid = self._run_checks_green()
        job_id = task_of(rid, "test")["job_id"]
        bad_test = TEST_OUT.replace(CANDIDATE, "sha256:" + "d" * 64)
        complete_job(job_id, bad_test)
        outcome = runs.on_task_result(job_id, True)
        self.assertEqual(outcome["state"], "retry")
        item = runs.get_run(rid)
        self.assertEqual(item["cycles"], 1)
        self.assertIsNone(item["candidate_digest"])
        self.assertIsNone(item.get("check_receipt"))
        self.assertEqual(next(t for t in item["tasks"] if t["key"] == "code")["state"], "pending")

    def test_failing_receipt_returns_to_coder_without_tester(self):
        """Receipt с упавшей проверкой: кодеру, модель тестировщика не тратится."""
        run = runs.create_run(self.project_id, plain_template(), {"max_cycles": 2})
        rid = run["run_id"]
        r = runs.dispatch(rid, resolve_agent)
        complete_job(r["dispatched"][0]["job_id"], PLAN_OUT)
        runs.on_task_result(r["dispatched"][0]["job_id"], True)
        r = runs.dispatch(rid, resolve_agent)
        complete_job(r["dispatched"][0]["job_id"], CODE_OUT)
        runs.on_task_result(r["dispatched"][0]["job_id"], True)
        runs.poll_active(resolve_agent)
        snap = [c for c in FAKE.created if c[0] == "snapshot"][-1]
        FAKE.set_result(snap[2], {"snapshot_id": "snap-cand", "tree_digest": CANDIDATE})
        runs.poll_active(resolve_agent)
        check = [c for c in FAKE.created if c[0] == "run_checks"][-1]
        bad = _green_receipt()
        bad["ok"] = False
        bad["checks"][0]["passed"] = False
        bad["checks"][0]["exit_code"] = 1
        FAKE.set_result(check[2], bad)
        runs.poll_active(resolve_agent)
        item = runs.get_run(rid)
        self.assertEqual(item["cycles"], 1)
        self.assertEqual(next(t for t in item["tasks"] if t["key"] == "code")["state"], "pending")
        # Тестировщик не выдавался: нет задания с receipt в prompt после сбоя.
        self.assertNotIn("test", [d["task"] for d in runs.dispatch(rid, resolve_agent)["dispatched"]])


class PacketIntegrityTest(DbTestCase):
    """Ревизии неизменяемы, digest от содержимого, materialize сверяет хост."""

    def _sealed(self, requested=("src/*",)):
        project = make_project()
        packet = packets.create(project["id"], list(requested), 100_000)
        entries = [
            {"path": "src/app.py", "size": 12, "sha256": _sha("v1")},
            {"path": "src/util.py", "size": 20, "sha256": _sha("u1")},
            {"path": "auth.json", "size": 9, "sha256": _sha("secret")},
        ]
        sealed = packets.seal(packet["packet_id"], entries)
        return project, packet["packet_id"], sealed

    def test_seal_recheck_conflict_new_revision_keeps_history(self):
        _, pid, first = self._sealed()
        self.assertEqual(first["revision"], 1)
        entries = [
            {"path": "src/app.py", "size": 12, "sha256": _sha("v1")},
            {"path": "src/util.py", "size": 20, "sha256": _sha("u1")},
            {"path": "auth.json", "size": 9, "sha256": _sha("secret")},
        ]
        same = packets.seal(pid, entries, recheck=True)
        self.assertFalse(same["changed"])
        self.assertEqual(same["state"], "sealed")
        # Тот же размер, другое содержимое: новая ревизия + conflict.
        changed = [dict(e) for e in entries]
        changed[0]["sha256"] = _sha("v2")
        conflict = packets.seal(pid, changed, recheck=True)
        self.assertEqual(conflict["state"], "conflict")
        self.assertEqual(conflict["revision"], 2)
        self.assertNotEqual(conflict["digest"], first["digest"])
        # Ревизия 1 в истории нетронута.
        history_v1 = packets.revision_items(pid, 1)
        app_v1 = next(i for i in history_v1 if i["path"] == "src/app.py")
        self.assertEqual(app_v1["sha256"], _sha("v1"))
        # А текущий указатель — новое содержимое.
        current = packets.get(pid)
        app_now = next(i for i in current["items"] if i["path"] == "src/app.py")
        self.assertEqual(app_now["sha256"], _sha("v2"))

    def test_auth_json_never_in_manifest(self):
        _, pid, sealed = self._sealed()
        items = packets.get(pid)["items"]
        auth = next(i for i in items if i["path"] == "auth.json")
        self.assertFalse(auth["selected"])

    def test_materialize_verifies_hashes(self):
        FAKE.patch()
        try:
            _, pid, sealed = self._sealed()
            started = packets.start_materialize(pid)
            self.assertEqual(started["state"], "materializing")
            job_id = started["job_id"]
            # Хост вернул ровно выбранные файлы с верными хешами.
            FAKE.set_result(job_id, {"files": [
                {"path": "src/app.py", "size": 12, "sha256": _sha("v1"), "text": "print(1)"},
                {"path": "src/util.py", "size": 20, "sha256": _sha("u1"), "text": "x"},
            ]})
            packets.poll_active()
            packet = packets.get(pid)
            self.assertEqual(packet["state"], "materialized")
            self.assertTrue(packet["materialized_digest"])
        finally:
            FAKE.restore()

    def test_materialize_rejects_spoofed_host(self):
        """Подменённый ответ хоста (другой hash, чужой файл) — conflict, не materialized."""
        FAKE.patch()
        try:
            _, pid, _ = self._sealed()
            started = packets.start_materialize(pid)
            FAKE.set_result(started["job_id"], {"files": [
                {"path": "src/app.py", "size": 12, "sha256": _sha("SPOOFED"), "text": "evil"},
                {"path": "src/util.py", "size": 20, "sha256": _sha("u1"), "text": "x"},
            ]})
            packets.poll_active()
            self.assertEqual(packets.get(pid)["state"], "conflict")
            self.assertNotEqual(packets.get(pid)["state"], "materialized")
            # Материализация не объявлена: materialized_at пуст.
            self.assertIsNone(packets.get(pid)["materialized_at"])

            # Чужой/невыбранный файл в ответе хоста тоже отклоняется.
            _, pid2, _ = self._sealed()
            started2 = packets.start_materialize(pid2)
            FAKE.set_result(started2["job_id"], {"files": [
                {"path": "src/app.py", "size": 12, "sha256": _sha("v1"), "text": "print(1)"},
                {"path": "src/util.py", "size": 20, "sha256": _sha("u1"), "text": "x"},
                {"path": "other/foreign.txt", "size": 3, "sha256": _sha("f"), "text": "no"},
            ]})
            packets.poll_active()
            packet2 = packets.get(pid2)
            self.assertEqual(packet2["state"], "conflict")
            self.assertIn("невыбранный", (packet2["manifest"].get("materialize") or {}).get("error", ""))
        finally:
            FAKE.restore()

    def test_materialize_refuses_secret_in_revision(self):
        """Ревизия с выбранным auth.json не материализуется: denied_path."""
        project = make_project()
        packet = packets.create(project["id"], ["src/*"], 100_000)
        entries = [{"path": "src/app.py", "size": 12, "sha256": _sha("v1")}]
        packets.seal(packet["packet_id"], entries)
        row = packets.get(packet["packet_id"])
        items = row["items"] + [{"path": "auth.json", "size": 9,
                                 "sha256": _sha("secret"), "selected": True,
                                 "reason": "tampered"}]
        with db._lock:
            conn = db.connect()
            packets._save_revision(conn, packet["packet_id"], 2,
                                   packets.digest(items), items)
            conn.run(db._qualify(
                "update aihub.context_packets set revision = 2 where id = :i"),
                i=packet["packet_id"])
        with self.assertRaises(packets.PacketError) as ctx:
            packets.start_materialize(packet["packet_id"])
        self.assertEqual(ctx.exception.code, "denied_path")


if __name__ == "__main__":
    unittest.main(verbosity=2)
