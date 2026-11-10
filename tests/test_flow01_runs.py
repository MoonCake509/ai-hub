"""FLOW-01: проверка шаблона запуска — юнит без базы."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import runs  # noqa: E402


class TemplateTest(unittest.TestCase):
    def test_accepts_simple_chain(self):
        steps = runs.validate_template({"steps": [
            {"key": "a"}, {"key": "b", "depends_on": ["a"]}]})
        self.assertEqual([s["key"] for s in steps], ["a", "b"])

    def test_rejects_empty(self):
        for template in ({}, {"steps": []}, {"steps": "x"}):
            with self.assertRaises(runs.RunError):
                runs.validate_template(template)

    def test_duplicate_key(self):
        with self.assertRaises(runs.RunError) as ctx:
            runs.validate_template({"steps": [{"key": "a"}, {"key": "a"}]})
        self.assertEqual(ctx.exception.code, "duplicate_step")

    def test_unknown_dependency(self):
        with self.assertRaises(runs.RunError) as ctx:
            runs.validate_template({"steps": [{"key": "a", "depends_on": ["ghost"]}]})
        self.assertEqual(ctx.exception.code, "unknown_dependency")

    def test_self_dependency(self):
        with self.assertRaises(runs.RunError) as ctx:
            runs.validate_template({"steps": [{"key": "a", "depends_on": ["a"]}]})
        self.assertEqual(ctx.exception.code, "self_dependency")

    def test_cycle_detected(self):
        with self.assertRaises(runs.RunError) as ctx:
            runs.validate_template({"steps": [
                {"key": "a", "depends_on": ["b"]},
                {"key": "b", "depends_on": ["c"]},
                {"key": "c", "depends_on": ["a"]}]})
        self.assertEqual(ctx.exception.code, "dependency_cycle")

    def test_step_without_key(self):
        with self.assertRaises(runs.RunError):
            runs.validate_template({"steps": [{"role": "coder"}]})

    def test_error_shape_is_serialisable(self):
        with self.assertRaises(runs.RunError) as ctx:
            runs.validate_template({"steps": [{"key": "a", "depends_on": ["ghost"]}]})
        payload = ctx.exception.as_dict()
        self.assertEqual(payload["code"], "unknown_dependency")
        self.assertIn("message", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)