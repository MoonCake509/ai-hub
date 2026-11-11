"""FLOW-P1: строгие типизированные схемы выходов ролей — юнит без базы.

Воспроизведения аудита: {"ok":true,"checks":[]} у тестировщика и
{"tasks":"not-a-list"} у планировщика не проходят типизированный контракт.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import templates  # noqa: E402


class PlannerSchemaTest(unittest.TestCase):
    def test_valid_plan_accepted(self):
        text = '{"tasks":[{"key":"k1","title":"сделать","files":["src/a.py"],"acceptance":"ok"}],"risks":[]}'
        ok, parsed, reason = templates.validate_output("planner", text)
        self.assertTrue(ok, reason)
        self.assertEqual(parsed["tasks"][0]["key"], "k1")

    def test_tasks_not_a_list_rejected(self):
        ok, _, reason = templates.validate_output("planner", '{"tasks":"not-a-list"}')
        self.assertFalse(ok)
        self.assertIn("tasks", reason)

    def test_task_without_key_rejected(self):
        ok, _, reason = templates.validate_output(
            "planner", '{"tasks":[{"title":"x","files":[]}]}')
        self.assertFalse(ok)
        self.assertIn("key", reason)

    def test_files_must_be_string_list(self):
        ok, _, reason = templates.validate_output(
            "planner", '{"tasks":[{"key":"k","title":"t","files":"src/a.py"}]}')
        self.assertFalse(ok)
        self.assertIn("files", reason)


class TesterSchemaTest(unittest.TestCase):
    def test_ok_true_with_empty_checks_rejected(self):
        """Аудит: «тест пройден» не принимается по пустому набору проверок."""
        ok, _, reason = templates.validate_output("tester", '{"ok":true,"checks":[]}')
        self.assertFalse(ok)
        self.assertIn("checks", reason)

    def test_candidate_digest_required(self):
        ok, _, reason = templates.validate_output(
            "tester", '{"ok":true,"checks":[{"name":"t","cmd":["pytest"],"ok":true}]}')
        self.assertFalse(ok)
        self.assertIn("candidate_digest", reason)

    def test_valid_evidence_accepted(self):
        ok, parsed, reason = templates.validate_output(
            "tester",
            '{"ok":true,"candidate_digest":"sha256:abc",'
            '"checks":[{"name":"unit","cmd":["pytest","-q"],"ok":true}]}')
        self.assertTrue(ok, reason)
        self.assertEqual(parsed["candidate_digest"], "sha256:abc")

    def test_check_ok_must_be_boolean(self):
        ok, _, reason = templates.validate_output(
            "tester",
            '{"ok":true,"candidate_digest":"x",'
            '"checks":[{"name":"t","cmd":"pytest","ok":"yes"}]}')
        self.assertFalse(ok)
        self.assertIn("boolean", reason)


class ReviewerSchemaTest(unittest.TestCase):
    def test_verdict_enum_enforced(self):
        ok, _, reason = templates.validate_output(
            "reviewer",
            '{"verdict":"looks-fine","candidate_digest":"x","findings":[]}')
        self.assertFalse(ok)
        self.assertIn("verdict", reason)

    def test_changes_requested_fails_transition(self):
        ok, _, reason = templates.validate_output(
            "reviewer",
            '{"verdict":"changes_requested","candidate_digest":"x","findings":[]}')
        self.assertFalse(ok)
        self.assertIn("changes_requested", reason)

    def test_findings_must_be_typed_objects(self):
        ok, _, reason = templates.validate_output(
            "reviewer",
            '{"verdict":"approve","candidate_digest":"x","findings":["bad"]}')
        self.assertFalse(ok)
        self.assertIn("findings", reason)


class ArchivistSchemaTest(unittest.TestCase):
    def test_artifacts_typed(self):
        ok, _, reason = templates.validate_output(
            "archivist", '{"summary":"итог","artifacts":[{"path":"docs/x.md","kind":"doc"}],"decisions":[]}')
        self.assertTrue(ok, reason)

    def test_artifact_requires_path_and_kind(self):
        ok, _, reason = templates.validate_output(
            "archivist", '{"summary":"s","artifacts":[{"path":"p"}],"decisions":[]}')
        self.assertFalse(ok)
        self.assertIn("kind", reason)


class CoderSchemaTest(unittest.TestCase):
    def test_valid_change_accepted(self):
        ok, _, reason = templates.validate_output(
            "coder", '{"summary":"сделано","files":["src/a.py"],"notes":null}')
        self.assertTrue(ok, reason)

    def test_missing_summary_rejected(self):
        ok, _, reason = templates.validate_output("coder", '{"files":[]}')
        self.assertFalse(ok)
        self.assertIn("summary", reason)

    def test_non_json_rejected(self):
        ok, _, reason = templates.validate_output("coder", "я всё сделал, честно")
        self.assertFalse(ok)
        self.assertIn("JSON", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
