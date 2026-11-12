"""P-09: семантический поиск — нарезка, вид фрагментов, ранжирование, проверка аргументов."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents" / "laptop"))
sys.path.insert(0, str(ROOT / "hub"))

import workspace_host as wh  # noqa: E402
from app import semantic, workspace  # noqa: E402


def test_kind_detection():
    assert wh._semantic_kind("hub/app/main.py") == "code"
    assert wh._semantic_kind("README.md") == "docs"
    assert wh._semantic_kind("config/agents.yaml") == "docs"
    assert wh._semantic_kind("logo.png") == ""


def test_chunks_cover_lines_with_overlap():
    text = "\n".join(f"line {i}" for i in range(1, 121))
    chunks = wh._chunk_lines(text, 40, 8)
    assert chunks[0][0] == 1 and chunks[0][1] == 40
    assert chunks[1][0] == 33, "второй фрагмент должен перекрывать первый"
    covered = set()
    for start, end, _ in chunks:
        covered.update(range(start, end + 1))
    assert covered == set(range(1, 121)), "все строки должны попасть хотя бы в один фрагмент"


def test_chunks_skip_blank_blocks():
    assert wh._chunk_lines("\n\n\n", 10, 2) == []


def test_cosine_prefers_close_vectors():
    near = semantic._cosine([1.0, 0.0], [0.9, 0.1])
    far = semantic._cosine([1.0, 0.0], [0.0, 1.0])
    assert near > 0.99 and abs(far) < 1e-9
    assert semantic._cosine([1.0], [1.0, 2.0]) == -1.0


def test_semantic_embed_validation():
    clean = workspace.validate("semantic_embed", {"root": str(ROOT), "text": "как работает max2", "kind": "code"})
    assert clean["kind"] == "code"
    with pytest.raises(workspace.OpError):
        workspace.validate("semantic_embed", {"root": str(ROOT), "text": "x", "kind": "image"})
    with pytest.raises(workspace.OpError):
        workspace.validate("semantic_embed", {"root": str(ROOT), "text": ""})


def test_semantic_index_limits():
    clean = workspace.validate("semantic_index", {"root": str(ROOT), "sha": "a" * 40, "max_chunks": 100,
                                                  "project_id": "p1", "upload": True})
    assert clean["max_chunks"] == 100 and clean["upload"] is True
    assert "items" not in clean
