"""FLOW-02: шаблон пяти ролей и строгие типизированные правила перехода."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import templates  # noqa: E402


class TemplateShapeTest(unittest.TestCase):
    def test_five_steps_in_order(self):
        tpl = templates.template()
        self.assertEqual([s["key"] for s in tpl["steps"]],
                         ["plan", "code", "test", "review", "archive"])
        self.assertEqual([s["role"] for s in tpl["steps"]],
                         ["planner", "coder", "tester", "reviewer", "archivist"])

    def test_dependencies_form_a_chain(self):
        steps = {s["key"]: s for s in templates.template()["steps"]}
        self.assertEqual(steps["plan"]["depends_on"], [])
        self.assertEqual(steps["code"]["depends_on"], ["plan"])
        self.assertEqual(steps["test"]["depends_on"], ["code"])
        self.assertEqual(steps["review"]["depends_on"], ["test"])
        self.assertEqual(steps["archive"]["depends_on"], ["review"])

    def test_each_role_gets_its_own_context(self):
        steps = templates.template()["steps"]
        signatures = {(tuple(s["context"]["paths"]), s["context"]["budget_tokens"]) for s in steps}
        self.assertGreater(len(signatures), 1)
        self.assertNotEqual(steps[0]["context"]["paths"], steps[1]["context"]["paths"])

    def test_template_passes_run_validation(self):
        from app import runs
        steps = runs.validate_template(templates.template())
        self.assertEqual(len(steps), 5)

    def test_tester_and_reviewer_require_candidate(self):
        """Выходы test/review связаны с кандидатом: схема требует его digest."""
        roles = {r["key"]: r for r in templates.describe()["roles"]}
        self.assertTrue(roles["tester"]["candidate"])
        self.assertTrue(roles["reviewer"]["candidate"])
        self.assertFalse(roles["planner"]["candidate"])


class OutputRulesTest(unittest.TestCase):
    def test_planner_requires_typed_tasks(self):
        ok, _, reason = templates.validate_output(
            "planner",
            '{"tasks":[{"key":"a","title":"сделать","files":["x.py"],"acceptance":"ok"}],"risks":[]}')
        self.assertTrue(ok, reason)
        ok, _, reason = templates.validate_output("planner", '{"risks":[]}')
        self.assertFalse(ok)
        self.assertIn("tasks", reason)
        # Строгость: задача без title/files не проходит.
        ok, _, reason = templates.validate_output("planner", '{"tasks":[{"key":"a"}]}')
        self.assertFalse(ok)
        self.assertIn("title", reason)

    def test_non_json_is_rejected(self):
        ok, parsed, reason = templates.validate_output("coder", "сделал всё хорошо")
        self.assertFalse(ok)
        self.assertIsNone(parsed)
        self.assertIn("JSON", reason)

    def test_json_in_code_fence_is_accepted(self):
        text = '```json\n{"summary":"ok","files":["a.py"]}\n```'
        ok, parsed, _ = templates.validate_output("coder", text)
        self.assertTrue(ok)
        self.assertEqual(parsed["summary"], "ok")

    def test_coder_files_must_be_string_list(self):
        ok, _, reason = templates.validate_output("coder", '{"summary":"s","files":"a.py"}')
        self.assertFalse(ok)
        self.assertIn("files", reason)

    def test_tester_must_prove(self):
        """Пустой набор проверок и отсутствие кандидата не принимаются."""
        ok, _, reason = templates.validate_output("tester", '{"ok":false,"checks":[]}')
        self.assertFalse(ok)
        self.assertIn("checks", reason)
        ok, _, reason = templates.validate_output(
            "tester", '{"ok":true,"checks":[{"name":"x","cmd":["t"],"ok":true}]}')
        self.assertFalse(ok)
        self.assertIn("candidate_digest", reason)
        ok, _, reason = templates.validate_output(
            "tester",
            '{"ok":true,"candidate_digest":"sha256:c",'
            '"checks":[{"name":"x","cmd":["t"],"ok":true}]}')
        self.assertTrue(ok, reason)

    def test_reviewer_must_approve_with_candidate(self):
        ok, _, reason = templates.validate_output(
            "reviewer", '{"verdict":"changes_requested","candidate_digest":"sha256:c","findings":[]}')
        self.assertFalse(ok)
        self.assertIn("изменения", reason)
        ok, _, reason = templates.validate_output(
            "reviewer", '{"verdict":"approve","candidate_digest":"sha256:c","findings":[]}')
        self.assertTrue(ok, reason)
        # Ревью без ссылки на кандидата не считается.
        ok, _, reason = templates.validate_output(
            "reviewer", '{"verdict":"approve","findings":[]}')
        self.assertFalse(ok)
        self.assertIn("candidate_digest", reason)

    def test_failure_returns_to_coder(self):
        self.assertEqual(templates.next_step_for_failure("tester"), "code")
        self.assertEqual(templates.next_step_for_failure("reviewer"), "code")
        self.assertIsNone(templates.next_step_for_failure("planner"))

    def test_unknown_role(self):
        ok, _, reason = templates.validate_output("wizard", "{}")
        self.assertFalse(ok)
        self.assertIn("неизвестная роль", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
