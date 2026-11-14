"""REASONING-01: уровень рассуждения Codex — каталог и флаг конкретного вызова.

Проверяется без запуска модели: каталог парсится из формы `codex debug models`,
а команда собирается с `-c model_reasoning_effort="<level>"` только когда
уровень выбран явно.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents" / "laptop"))

import bridge  # noqa: E402


CATALOG_FIXTURE = {
    "models": [
        {
            "slug": "gpt-6.1-sol",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "fast"},
                {"effort": "medium", "description": "balanced"},
                {"effort": "high", "description": "deep"},
                {"effort": "ultra", "description": "max"},
            ],
        },
        {"slug": "gpt-6-luna", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
        {"slug": "broken"},
    ]
}


def test_catalog_parses_levels_and_defaults():
    models = bridge._parse_codex_catalog(CATALOG_FIXTURE)
    assert models["gpt-6.1-sol"] == {
        "default": "low", "levels": ["low", "medium", "high", "ultra"]}
    assert models["gpt-6-luna"]["levels"] == ["low", "medium"]
    assert models["broken"]["levels"] == []


def _capture_run(monkeypatch, reasoning_effort):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["input"] = kwargs.get("input")
        return types.SimpleNamespace(returncode=0, stdout=b'{"type":"done"}\n', stderr=b"")

    monkeypatch.setattr(bridge, "resolve_codex_exe", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    result = bridge.run_codex("hi", 5, "gpt-6.1-sol", None,
                              reasoning_effort=reasoning_effort)
    return seen, result


def test_explicit_level_is_passed_as_config_override(monkeypatch):
    seen, result = _capture_run(monkeypatch, "high")
    assert result["exit_code"] == 0
    assert "-c" in seen["cmd"]
    idx = seen["cmd"].index("-c")
    assert seen["cmd"][idx + 1] == 'model_reasoning_effort="high"'
    # Уровень идёт отдельным флагом, а не переписывает глобальный конфиг.
    assert not any("config.toml" in str(part) for part in seen["cmd"])


def test_default_level_adds_no_override(monkeypatch):
    seen, _ = _capture_run(monkeypatch, None)
    assert "-c" not in seen["cmd"]


def test_invalid_level_is_rejected_before_process_start(monkeypatch):
    with pytest.raises(ValueError):
        _capture_run(monkeypatch, "high; rm -rf /")


def test_catalog_cache_is_reused(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(CATALOG_FIXTURE).encode(),
                                     stderr=b"")

    monkeypatch.setattr(bridge, "resolve_codex_exe", lambda: r"C:\fake\codex.exe")
    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(bridge.os.path, "getmtime", lambda _p: 1.0)
    bridge._CATALOG_CACHE.update(key=None, at=0.0, data=None)
    first = bridge.codex_model_catalog(force=True)
    second = bridge.codex_model_catalog()
    assert first["available"] and second["available"]
    assert calls["n"] == 1
