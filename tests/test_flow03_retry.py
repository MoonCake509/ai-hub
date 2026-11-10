"""FLOW-03: правило возврата к кодеру и лимит циклов."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import runs  # noqa: E402


class RetryDecisionTest(unittest.TestCase):
    def test_tester_failure_returns_to_coder(self):
        result = runs.retry_decision("tester", 0, {"max_cycles": 2})
        self.assertEqual(result["action"], "retry_coder")
        self.assertEqual(result["next_cycle"], 1)

    def test_reviewer_failure_returns_to_coder(self):
        result = runs.retry_decision("reviewer", 1, {"max_cycles": 2})
        self.assertEqual(result["action"], "retry_coder")
        self.assertEqual(result["next_cycle"], 2)

    def test_limit_stops_with_reason(self):
        result = runs.retry_decision("tester", 2, {"max_cycles": 2})
        self.assertEqual(result["action"], "fail")
        self.assertIn("лимит циклов", result["reason"])

    def test_no_policy_means_no_cycles(self):
        result = runs.retry_decision("tester", 0, {})
        self.assertEqual(result["action"], "fail")
        self.assertIn("не разрешает циклы", result["reason"])

    def test_planner_failure_is_final(self):
        result = runs.retry_decision("planner", 0, {"max_cycles": 3})
        self.assertEqual(result["action"], "fail")

    def test_unknown_role_is_final(self):
        result = runs.retry_decision("", 0, {"max_cycles": 3})
        self.assertEqual(result["action"], "fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)