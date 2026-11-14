"""Этап 5: режим размышления у провайдера «qwen (сервер)».

Проблема была системной: в матрице возможностей не было измерения `thinking`,
поэтому UI физически нечего было показывать, а preflight отклонил бы запрос.
Проверяем: измерение существует, у qwen-server оно подтверждено источником,
уровни отдаются интерфейсу, адаптер принимает режим и совместим со старым
вызовом без режима.

Запуск: python tests/test_qwen_server_thinking.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def load_capabilities():
    spec = importlib.util.spec_from_file_location("caps", ROOT / "hub" / "app" / "capabilities.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    caps = load_capabilities()
    check("thinking в CAPABILITIES", "thinking" in caps.CAPABILITIES)
    check("thinking имеет подпись", "thinking" in caps.LABELS)
    check("thinking имеет причину отказа", "thinking" in caps.DEFAULT_REASON)

    agents = {a["id"]: a for a in yaml.safe_load(
        (ROOT / "config" / "agents.yaml").read_text(encoding="utf-8"))["agents"]}
    qwen = agents["qwen-server"]
    declared = (qwen.get("capabilities") or {}).get("thinking") or {}
    check("qwen-server объявляет thinking", declared.get("supported") is True)
    check("источник — fixture, а не догадка", str(declared.get("evidence", "")).startswith("fixture:"))

    matrix = caps.resolve(qwen)
    node = (matrix.get("capabilities") or {}).get("thinking") or {}
    check("матрица показывает thinking включённым", node.get("supported") is True)

    main_py = (ROOT / "hub" / "app" / "main.py").read_text(encoding="utf-8")
    check("уровни Qwen объявлены в хабе", "QWEN_THINKING_LEVELS" in main_py)
    check("каталог воркера главнее объявленного", "workers.reasoning_catalog(agent_id) or declared_reasoning(item)" in main_py)
    check("валидация использует объявленные уровни", "reasoning_levels_for" in main_py)

    qwen_js = (ROOT / "agents" / "laptop" / "qwen.js").read_text(encoding="utf-8")
    check("CLI понимает старый вызов без режима", "QWEN_THINKING_MODES.has(String(tail[0]).trim().toLowerCase())" in qwen_js)

    worker = (ROOT / "agents" / "server" / "worker_v2.py").read_text(encoding="utf-8")
    check("серверный воркер маппит уровень в режим", "def qwen_desktop_mode" in worker)
    check("low/fast -> fast", 'return "fast"' in worker)
    check("medium/high/max -> thinking", 'return "thinking"' in worker)
    check("пусто -> auto", 'return "auto"' in worker)
    check("режим передаётся адаптеру",
          '"ask-json", job.get("requested_model") or "", mode, prompt' in worker)

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
