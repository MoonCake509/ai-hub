"""REASONING-01: хаб проверяет уровень по каталогу воркера до постановки задания."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hub"))

from app import workers  # noqa: E402


def _catalog(monkeypatch, models):
    monkeypatch.setattr(workers, "reasoning_catalog", lambda agent_id, at=None: {
        "models": models, "worker_id": "w", "cli_version": "0.166.0", "state": "ok"})


def test_levels_come_from_worker_catalog(monkeypatch):
    _catalog(monkeypatch, {"gpt-6.1-sol": {"default": "low",
                                          "levels": ["low", "medium", "high", "xhigh", "max", "ultra"]}})
    assert workers.reasoning_levels("codex", "gpt-6.1-sol") == [
        "low", "medium", "high", "xhigh", "max", "ultra"]


def test_unknown_model_has_no_levels(monkeypatch):
    _catalog(monkeypatch, {"gpt-6.1-sol": {"default": "low", "levels": ["low"]}})
    assert workers.reasoning_levels("codex", "gpt-unknown") == []


def test_agent_without_catalog_rejects_any_level(monkeypatch):
    monkeypatch.setattr(workers, "reasoning_catalog", lambda agent_id, at=None: None)
    assert workers.reasoning_levels("hermes-laptop", "deepseek-v4.1-flash") == []
