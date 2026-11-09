from __future__ import annotations
import sys
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
import asyncio
from types import SimpleNamespace

APP_DIR = Path(__file__).resolve().parents[1] / "hub"
sys.path.insert(0, str(APP_DIR))
from app import quality, db, quality_db  # noqa: E402
from app import main  # noqa: E402


class DeterministicGradeTests(unittest.TestCase):
    def case(self, case_id):
        return next(case for case in quality.get_suite()["cases"] if case["id"] == case_id)

    def test_exact_instruction_passes_only_exact_payload(self):
        case = self.case("exact-instruction")
        self.assertTrue(quality.grade(case, "ROUTE-OK")["passed"])
        self.assertFalse(quality.grade(case, "ROUTE-OK\nextra")["passed"])

    def test_json_shape_is_checked_without_execution(self):
        case = self.case("json-shape")
        self.assertTrue(quality.grade(case, '{"status":"ready","count":3}') ["passed"])
        self.assertFalse(quality.grade(case, '{"status":"ready","count":3,"extra":true}') ["passed"])
        self.assertFalse(quality.grade(case, 'not json')["passed"])

    def test_math_case_rejects_non_integer_text(self):
        case = self.case("basic-math")
        self.assertTrue(quality.grade(case, "14")["passed"])
        self.assertFalse(quality.grade(case, "The answer is 14")["passed"])

    def test_synthetic_context_retrieval_is_exact(self):
        case = self.case("synthetic-context")
        self.assertTrue(quality.grade(case, "MINT-48A")["passed"])
        self.assertFalse(quality.grade(case, "MINT-48B")["passed"])

    def test_suite_and_grader_versions_are_snapshotted(self):
        suite = quality.get_suite()
        self.assertEqual(suite["version"], "1.0.0")
        self.assertEqual(suite["grader_version"], "deterministic-v1")
        self.assertEqual(len(suite["cases"]), 4)

    def test_selected_case_count_is_bounded_and_explicit_ids_preserve_order(self):
        with self.assertRaises(ValueError):
            quality.select_cases(5)
        selected = quality.select_cases(2, ["basic-math", "exact-instruction"])
        self.assertEqual([item["id"] for item in selected], ["basic-math", "exact-instruction"])

    def test_channel_key_is_stable_and_tuple_specific(self):
        first = quality.channel_id("codex", "openai", "gpt-6-luna")
        self.assertEqual(first, quality.channel_id("codex", "openai", "gpt-6-luna"))
        self.assertNotEqual(first, quality.channel_id("codex", "openai", "gpt-6-sol"))

    def test_channel_catalog_deduplicates_route_and_marks_image_required(self):
        profiles = {"ocr": {"profiles": [{"id": "local", "name": "Local", "models": [
            {"id": "vision", "name": "Vision", "requires_image": True, "required_modalities": ["image"]},
            {"id": "vision", "name": "Duplicate entry"},
        ]}]}}
        agents = [{"id": "ocr", "name": "OCR", "model_selection": True}]
        with patch("app.quality_db.list_channels", return_value=[]), \
             patch("app.quality_db.channel_audit", return_value=[]):
            catalog = quality.channel_catalog(profiles, agents)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["model_name"], "Vision")
        self.assertTrue(catalog[0]["requires_image"])
        self.assertEqual(catalog[0]["required_modalities"], ["image"])

    def test_channel_catalog_reads_profile_model_capability_for_image_filter(self):
        profiles = {"ocr": {"profiles": [{
            "id": "local", "name": "Local", "models": ["vision"],
            "model_capabilities": {"vision": {
                "requires_image": True, "required_modalities": ["image"],
            }},
        }]}}
        agents = [{"id": "ocr", "name": "OCR", "model_selection": True}]
        with patch("app.quality_db.list_channels", return_value=[]), \
             patch("app.quality_db.channel_audit", return_value=[]):
            catalog = quality.channel_catalog(profiles, agents)
        self.assertEqual(len(catalog), 1)
        self.assertTrue(catalog[0]["requires_image"])
        self.assertEqual(catalog[0]["required_modalities"], ["image"])

    def test_missing_attempt_usage_stays_unavailable(self):
        self.assertIsNone(quality._sum_attempt_usage([{"usage": None}]))
        partial = quality._sum_attempt_usage([
            {"usage": {"input_tokens": 10, "output_tokens": 4}},
            {"usage": {"input_tokens": 2}},
        ])
        self.assertEqual(partial, {"input_tokens": 12})

    def test_disabled_channel_rejects_new_work(self):
        with patch("app.quality_db.get_channel", return_value={"enabled": False, "reason": "weak output"}):
            with self.assertRaisesRegex(ValueError, "weak output"):
                quality.ensure_channel_enabled("codex", "openai", "gpt-6-luna")

    def test_partial_run_has_live_counts_but_no_final_score_or_fake_total_usage(self):
        now = datetime.now(timezone.utc)
        run = {
            "id": "run-1", "created_at": now,
            "items": [
                {"status": "succeeded", "passed": True, "queue_wait_seconds": 1.0,
                 "execution_seconds": 2.0, "first_started_at": now, "finished_at": now,
                 "usage": {"input_tokens": 20, "output_tokens": 5}},
                {"status": "running", "passed": None, "queue_wait_seconds": 1.0,
                 "execution_seconds": 1.0, "first_started_at": now, "finished_at": None, "usage": None},
            ],
        }
        with patch("app.quality_db.update_run_aggregate") as update:
            quality._aggregate(run)
        values = update.call_args.args[1]
        self.assertEqual(values["status"], "running")
        self.assertEqual(values["passed_count"], 1)
        self.assertEqual(values["failed_count"], 0)
        self.assertIsNone(values["score"])
        self.assertIsNone(values["total_usage"])
        self.assertIsNotNone(values["wall_seconds"])

    def test_unmeasured_cost_and_ttft_are_null(self):
        result = quality.public_run({"id": "run-1", "items": []})
        self.assertIsNone(result["cost"])
        self.assertIsNone(result["first_token_latency_seconds"])

    def test_completed_job_result_keeps_source_timings_and_reported_model_separate(self):
        from datetime import timedelta
        ended = datetime.now(timezone.utc)
        started = ended - timedelta(seconds=4)
        created = started - timedelta(seconds=3)
        item = {
            "id": "item-1", "job_id": "job-1",
            "case_snapshot": self.case("basic-math"), "status": "running",
        }
        job = {
            "id": "job-1", "status": "succeeded", "created_at": created,
            "finished_at": ended, "result_output": "14", "effective_model": "reported-luna",
            "attempts_detail": [{
                "started_at": started, "finished_at": ended,
                "usage": {"input_tokens": 11, "output_tokens": 2, "cached_input_tokens": 3,
                          "reasoning_output_tokens": 1, "model": "reported-luna"},
            }],
        }
        with patch.object(db, "get_job", return_value=job), \
             patch.object(quality_db, "update_item") as save:
            quality._refresh_item("run-1", item)
        values = save.call_args.args[1]
        self.assertTrue(values["passed"])
        self.assertEqual(values["raw_response"], "14")
        self.assertEqual(values["reported_model"], "reported-luna")
        self.assertEqual(values["identity_evidence_source"], "adapter_usage.model")
        self.assertAlmostEqual(values["queue_wait_seconds"], 3.0, delta=0.02)
        self.assertAlmostEqual(values["execution_seconds"], 4.0, delta=0.02)
        self.assertAlmostEqual(values["wall_seconds"], 7.0, delta=0.02)
        self.assertEqual(values["usage"]["input_tokens"], 11)
        self.assertIsNone(values["cost"])
        self.assertIsNone(values["first_token_latency_seconds"])


class DisabledChannelRouteTests(unittest.TestCase):
    class Request:
        def __init__(self, payload):
            self.payload = payload
            self.headers = {}

        async def json(self):
            return self.payload

    def test_disabled_channel_blocks_standard_agent_job_creation(self):
        agent = {"id": "codex", "bridge_agent": "codex", "model_selection": True}
        with patch.object(main, "find_agent", return_value=agent), \
             patch.object(main, "AGENT_PROFILES", {"codex": {
                 "default_profile": "openai", "profiles": [{"id": "openai", "models": ["gpt-6-luna"]}],
             }}), \
             patch.object(main.quality, "ensure_channel_enabled", side_effect=ValueError("channel disabled")), \
             patch.object(main.db, "create_job") as create_job:
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.run_agent("codex", self.Request({"prompt": "hello", "timeout": 60})))
        self.assertEqual(raised.exception.status_code, 409)
        create_job.assert_not_called()

    def test_disabled_channel_blocks_workspace_chat_job_creation(self):
        request = self.Request({"text": "hello", "project_id": "project-1"})
        agent = {"id": "codex", "bridge_agent": "codex", "model_selection": True}
        with patch.object(main.db, "get_conversation", return_value={
                 "id": "conversation-1", "project_id": "project-1", "role_id": "architect",
             }), \
             patch.object(main.db, "get_project", return_value={
                 "id": "project-1", "host_id": "laptop", "workdir": "",
             }), \
             patch.object(main, "_resolve_chat_target", return_value=(
                 agent, {}, "codex", "openai", "gpt-6-luna", 60,
             )), \
             patch.object(main, "_validate_chat_profile", return_value=("openai", "gpt-6-luna")), \
             patch.object(main.quality, "ensure_channel_enabled", side_effect=ValueError("channel disabled")), \
             patch.object(main.db, "get_job_by_idempotency_key", return_value=None), \
             patch.object(main.db, "create_chat_job") as create_job:
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.conversation_send("conversation-1", request))
        self.assertEqual(raised.exception.status_code, 409)
        create_job.assert_not_called()

    def test_channel_toggle_requires_and_persists_audit_reason(self):
        channel = {"channel_id": "chan-1", "agent_id": "codex", "profile": "openai", "model": "gpt-6-luna"}
        audit = [{"channel_id": "chan-1", "enabled_before": True, "enabled_after": False, "reason": "weak output"}]
        with patch.object(main.quality, "channel_catalog", return_value=[channel]), \
             patch.object(main.quality_db, "set_channel", return_value={**channel, "enabled": False, "reason": "weak output"}) as save, \
             patch.object(main.quality_db, "channel_audit", return_value=audit):
            response = asyncio.run(main.quality_update_channel(self.Request({
                "channel_id": "chan-1", "enabled": False, "reason": "weak output",
            })))
        save.assert_called_once_with(channel, False, "weak output")
        self.assertEqual(response["audit"], audit)

    def test_quality_run_enqueues_one_normal_job_per_selected_case(self):
        cases = quality.select_cases(2)
        run = {"id": "quality-run-1", "items": [
            {"id": "item-1", "case_id": cases[0]["id"], "case_snapshot": cases[0]},
            {"id": "item-2", "case_id": cases[1]["id"], "case_snapshot": cases[1]},
        ]}
        jobs = iter([{"id": "job-1", "status": "queued"}, {"id": "job-2", "status": "queued"}])
        def attach(item_id, job):
            row = next(item for item in run["items"] if item["id"] == item_id)
            row.update(job_id=job["id"], status=job["status"], job_status=job["status"])
        request = self.Request({
            "agent_id": "codex", "profile": "openai", "model": "gpt-6-luna",
            "case_count": 2, "timeout_s": 45, "idempotency_key": "quality-req-1",
        })
        with patch.object(main, "_resolve_quality_channel", return_value=(
                 {"id": "codex", "bridge_agent": "codex"}, "openai", "gpt-6-luna",
             )), \
             patch.object(main.quality, "channel_state", return_value={"channel_id": "channel-1"}), \
             patch.object(main.quality, "ensure_channel_enabled"), \
             patch.object(main.quality_db, "get_run_by_key", return_value=None), \
             patch.object(main.quality_db, "create_run", return_value=(run, True)), \
             patch.object(main.quality_db, "attach_job", side_effect=attach), \
             patch.object(main.quality_db, "mark_item_enqueue_error"), \
             patch.object(main.quality_db, "set_run_error"), \
             patch.object(main.db, "create_job", side_effect=lambda *args, **kwargs: (next(jobs), True)) as create_job, \
             patch.object(main.db, "get_job", return_value=None), \
             patch.object(main.quality, "refresh_job"), \
             patch.object(main.quality, "refresh_run", return_value=run):
            response = asyncio.run(main.quality_start_run(request))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(create_job.call_count, 2)
        self.assertEqual([call.args[1] for call in create_job.call_args_list], [case["prompt"] for case in cases])
        self.assertTrue(all(call.args[8]["quality_run_id"] == "quality-run-1" for call in create_job.call_args_list))

    def test_quality_text_suite_rejects_image_required_channel_before_run_or_job_creation(self):
        request = self.Request({
            "agent_id": "xiaomi-ocr-server", "profile": "local", "model": "xiaomi-ocr-q8",
            "case_count": 1, "timeout_s": 45, "idempotency_key": "quality-image-must-reject",
        })
        with patch.object(main, "_resolve_quality_channel", return_value=(
                 {"id": "xiaomi-ocr-server", "bridge_agent": "xiaomi-ocr-server", "transport": "http"},
                 "local", "xiaomi-ocr-q8",
             )), \
             patch.object(main, "_model_capabilities", return_value={
                 "requires_image": True, "required_modalities": ["image"],
             }), \
             patch.object(main.quality, "channel_state", return_value={"channel_id": "xiaomi"}), \
             patch.object(main.quality_db, "get_run_by_key", return_value=None), \
             patch.object(main.quality_db, "create_run") as create_run, \
             patch.object(main.db, "create_job") as create_job:
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.quality_start_run(request))
        self.assertEqual(raised.exception.status_code, 400)
        create_run.assert_not_called()
        create_job.assert_not_called()

    def test_quality_run_idempotent_replay_returns_before_new_channel_capability_guard(self):
        request = self.Request({
            "agent_id": "xiaomi-ocr-server", "profile": "local", "model": "xiaomi-ocr-q8",
            "case_count": 1, "timeout_s": 45, "idempotency_key": "already-accepted-image-run",
        })
        existing = {"id": "old-run", "suite_id": quality.SUITE_ID,
                    "suite_version": quality.SUITE_VERSION, "channel_id": "xiaomi-channel",
                    "timeout_s": 45, "items": [{"case_id": "exact-instruction"}]}
        with patch.object(main, "_resolve_quality_channel", return_value=(
                 {"id": "xiaomi-ocr-server", "bridge_agent": "xiaomi-ocr-server", "transport": "http"},
                 "local", "xiaomi-ocr-q8",
             )), \
             patch.object(main.quality, "channel_state", return_value={"channel_id": "xiaomi-channel"}), \
             patch.object(main.quality_db, "get_run_by_key", return_value=existing), \
             patch.object(main.quality, "refresh_run", return_value=existing), \
             patch.object(main, "_model_capabilities", side_effect=AssertionError("new-run guard must not run on replay")), \
             patch.object(main.db, "create_job") as create_job:
            response = asyncio.run(main.quality_start_run(request))
        self.assertEqual(response.status_code, 200)
        create_job.assert_not_called()


if __name__ == "__main__":
    unittest.main()
