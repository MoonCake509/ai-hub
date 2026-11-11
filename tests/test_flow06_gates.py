"""FLOW-P1: программные ворота процесса — юнит без базы и сети.

Модель не может объявить тесты пройденными: receipt изолированного runner'а
обязан иметь ненулевой набор проверок, exit code 0 у каждой, тот же snapshot
кандидата и подтверждённую изоляцию. Ревью обязано ссылаться на того же
кандидата.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import flow_gates  # noqa: E402

SNAP = "candidate-abc"
ISOLATION = {
    "network": "none", "rootfs_read_only": True, "cap_drop_all": True,
    "no_new_privileges": True, "resource_limits": True,
    "snapshot_read_only": True, "nonroot": True, "ephemeral_workspace": True,
    "image": "sha256:" + "a" * 64,
}


def receipt(ok=True, exit_code=0, checks=None, snapshot_id=SNAP, isolation=ISOLATION):
    items = checks if checks is not None else [
        {"name": "unit", "cmd": ["pytest", "-q"], "exit_code": exit_code, "passed": ok},
    ]
    body = {"snapshot_id": snapshot_id, "ok": ok, "checks": items}
    if isolation is not None:
        body["isolation"] = isolation
    return body


class ValidateReceiptTest(unittest.TestCase):
    def test_valid_receipt_accepted(self):
        result = flow_gates.validate_check_receipt(receipt(), snapshot_id=SNAP)
        self.assertEqual(len(result["checks"]), 1)

    def test_no_receipt_rejected(self):
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(None, snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "no_receipt")

    def test_empty_checks_rejected(self):
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(receipt(checks=[]), snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "no_checks")

    def test_nonzero_exit_rejected_even_if_model_says_ok(self):
        bad = receipt(ok=True, exit_code=1)
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(bad, snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "check_failed")

    def test_failed_check_rejected(self):
        bad = {
            "snapshot_id": SNAP, "ok": False,
            "checks": [{"name": "unit", "cmd": ["pytest"], "exit_code": 0, "passed": False}],
            "isolation": ISOLATION,
        }
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(bad, snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "check_failed")

    def test_other_snapshot_rejected(self):
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(receipt(snapshot_id="other-snap"), snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "candidate_mismatch")

    def test_missing_isolation_rejected(self):
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(receipt(isolation=None), snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "isolation_unverified")

    def test_weakened_isolation_rejected(self):
        weak = dict(ISOLATION, network="bridge")
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(receipt(isolation=weak), snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "isolation_unverified")

    def test_exit_code_must_be_int(self):
        bad = receipt()
        bad["checks"][0]["exit_code"] = "0"
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.validate_check_receipt(bad, snapshot_id=SNAP)
        self.assertEqual(ctx.exception.code, "bad_receipt")


class TesterGateTest(unittest.TestCase):
    def test_candidate_digest_must_match(self):
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.gate_tester_output({"candidate_digest": "sha256:other"},
                                          candidate_digest="sha256:mine")
        self.assertEqual(ctx.exception.code, "candidate_mismatch")

    def test_matching_digest_passes(self):
        flow_gates.gate_tester_output({"candidate_digest": "sha256:mine"},
                                      candidate_digest="sha256:mine")


class ReviewerGateTest(unittest.TestCase):
    def test_review_of_other_candidate_rejected(self):
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.gate_reviewer_output(
                {"verdict": "approve", "candidate_digest": "sha256:old"},
                candidate_digest="sha256:new")
        self.assertEqual(ctx.exception.code, "candidate_mismatch")

    def test_non_approve_rejected(self):
        with self.assertRaises(flow_gates.GateError) as ctx:
            flow_gates.gate_reviewer_output(
                {"verdict": "changes_requested", "candidate_digest": "sha256:same"},
                candidate_digest="sha256:same")
        self.assertEqual(ctx.exception.code, "changes_requested")


if __name__ == "__main__":
    unittest.main(verbosity=2)
