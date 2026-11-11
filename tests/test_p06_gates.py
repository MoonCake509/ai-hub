"""P-06: ворота кандидата не зависят от формы записи digest.

Тестировщик и ревьюер возвращают `sha256:<hex>` или `<hex>`; сравнение должно
принимать обе формы и отвергать чужой кандидат.
"""
import pytest

from hub.app import flow_gates


DIGEST = "fbf86093d154023136f29637231521e0a48522de85a2d9c1443918ae5986a93d"


def test_tester_accepts_prefixed_digest():
    flow_gates.gate_tester_output({"candidate_digest": f"sha256:{DIGEST}".upper(),
                                   "ok": True}, candidate_digest=DIGEST)


def test_tester_accepts_plain_digest():
    flow_gates.gate_tester_output({"candidate_digest": DIGEST, "ok": True},
                                  candidate_digest=f"sha256:{DIGEST}")


def test_tester_rejects_other_candidate():
    with pytest.raises(flow_gates.GateError) as exc:
        flow_gates.gate_tester_output({"candidate_digest": "sha256:" + "0" * 64},
                                      candidate_digest=DIGEST)
    assert exc.value.code == "candidate_mismatch"


def test_reviewer_requires_approve():
    flow_gates.gate_reviewer_output({"candidate_digest": f"sha256:{DIGEST}",
                                     "verdict": "approve"}, candidate_digest=DIGEST)
    with pytest.raises(flow_gates.GateError) as exc:
        flow_gates.gate_reviewer_output({"candidate_digest": f"sha256:{DIGEST}",
                                         "verdict": "request_changes"}, candidate_digest=DIGEST)
    assert exc.value.code == "changes_requested"