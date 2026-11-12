"""P-09: проверяемый семантический контекст — evidence, digest, сверка blob'ов."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents" / "laptop"))
sys.path.insert(0, str(ROOT / "hub"))

import workspace_host as wh  # noqa: E402
from app import packets, semantic  # noqa: E402


def _evidence(path="app.py", start=1, end=3, oid="a" * 40, digest="b" * 64):
    return {"sha": "c" * 40, "path": path, "start_line": start, "end_line": end,
            "kind": "code", "text": "x = 1\ny = 2\n", "blob_oid": oid,
            "text_sha256": digest, "score": 0.5, "model": "e5-mean"}


def test_digest_changes_with_evidence():
    items = [{"path": "app.py", "size": 10, "sha256": "d" * 64, "selected": True}]
    plain = packets.digest(items)
    with_ev = packets.digest(items, evidence=[_evidence()])
    assert plain != with_ev
    assert packets.digest(items, evidence=[_evidence()]) == with_ev, "digest детерминирован"


def test_evidence_canonical_ignores_text_and_score():
    left = _evidence()
    right = dict(left, text="другой текст", score=0.9)
    assert packets.evidence_canonical([left]) == packets.evidence_canonical([right])


def test_evidence_canonical_responds_to_oid_and_hash():
    left = _evidence()
    assert packets.evidence_canonical([left]) != packets.evidence_canonical([_evidence(oid="e" * 40)])
    assert packets.evidence_canonical([left]) != packets.evidence_canonical([_evidence(digest="f" * 64)])


def test_overlap_dedup_keeps_single_fragment():
    first = {"path": "app.py", "start_line": 1, "end_line": 40}
    assert semantic._overlaps(first, {"path": "app.py", "start_line": 35, "end_line": 80})
    assert not semantic._overlaps(first, {"path": "app.py", "start_line": 60, "end_line": 90})
    assert not semantic._overlaps(first, {"path": "other.py", "start_line": 1, "end_line": 40})


def test_collect_evidence_keeps_only_verified(monkeypatch):
    monkeypatch.setattr(semantic, "kinds", lambda project_id, sha="": ["code"])
    calls = []

    def fake_op(project, op, args, timeout_s=90):
        calls.append(op)
        if op == "semantic_embed":
            return {"vector": [1.0, 0.0]}
        return {"sha": args["sha"], "dropped": [{"path": "gone.py", "reason": "not_in_sha"}],
                "verified": [{"sha": args["sha"], "path": "app.py", "start_line": 1,
                              "end_line": 40, "kind": "code", "blob_oid": "a" * 40,
                              "text": "x = 1\n" * 20,
                              "text_sha256": "b" * 64}]}

    monkeypatch.setattr(semantic, "_op", fake_op)
    monkeypatch.setattr(semantic, "search", lambda *a, **k: {"matches": [
        {"path": "app.py", "start_line": 1, "end_line": 40, "kind": "code", "score": 0.8},
        {"path": "app.py", "start_line": 30, "end_line": 60, "kind": "code", "score": 0.7},
    ]})
    items, state = semantic.collect_evidence({"id": "p", "workdir": "R:/repo"}, "c" * 40, "максимум")
    assert calls == ["semantic_embed", "verify_evidence"], "вектор и проверка — отдельными операциями"
    assert [item["path"] for item in items] == ["app.py"], "перекрытие дедуплицировано"
    assert state["state"] == "collected" and state["dropped"] == 1


def test_collect_evidence_without_index_is_not_fatal(monkeypatch):
    monkeypatch.setattr(semantic, "kinds", lambda project_id, sha="": [])
    items, state = semantic.collect_evidence({"id": "p"}, "c" * 40, "цель")
    assert items == [] and state["state"] == "no_index"


def test_collect_evidence_reports_embed_failure(monkeypatch):
    monkeypatch.setattr(semantic, "kinds", lambda project_id, sha="": ["code"])

    def broken(*a, **k):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(semantic, "_op", broken)
    items, state = semantic.collect_evidence({"id": "p"}, "c" * 40, "цель")
    assert items == [] and state["state"] == "embed_failed"


def _git(repo, *args):
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_verify_evidence_matches_blob_and_range(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "app.py")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    sha = _git(repo, "rev-parse", "HEAD")
    oid = _git(repo, "rev-parse", f"HEAD:app.py")

    result = wh.op_verify_evidence(str(repo), {"sha": sha, "items": [
        {"path": "app.py", "start_line": 2, "end_line": 3, "kind": "code"}]})
    assert len(result["verified"]) == 1
    item = result["verified"][0]
    assert item["text"] == "b = 2\nc = 3"
    assert item["blob_oid"] == oid
    assert item["text_sha256"] == __import__("hashlib").sha256(item["text"].encode()).hexdigest()

    bad = wh.op_verify_evidence(str(repo), {"sha": sha, "items": [
        {"path": "app.py", "start_line": 3, "end_line": 99, "kind": "code"},
        {"path": "gone.py", "start_line": 1, "end_line": 2, "kind": "code"}]})
    assert bad["verified"] == []
    reasons = {row["reason"] for row in bad["dropped"]}
    assert reasons == {"range_out_of_bounds", "not_in_sha"}
