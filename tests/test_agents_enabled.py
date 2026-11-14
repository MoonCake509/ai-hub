"""Этап 2: в реестре остаются только серверный контур и Codex.

Ноутбучные дубли (Qwen/OpenClaw/Hermes) выключены флагом, а не удалены:
на них ссылаются история, роли и уже созданные чаты. Интерфейс не должен
предлагать выключенный канал ни в запуске, ни в редакторе роли.

Запуск: python tests/test_agents_enabled.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("ok   " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    agents = {a["id"]: a for a in yaml.safe_load(
        (ROOT / "config" / "agents.yaml").read_text(encoding="utf-8"))["agents"]}

    for aid in ("qwen-code", "openclaw-laptop", "hermes-laptop"):
        check(f"{aid} выключен", agents.get(aid, {}).get("enabled") is False)
    for aid in ("codex", "qwen-server", "hermes-server", "openclaw-server"):
        check(f"{aid} остаётся включённым", agents.get(aid, {}).get("enabled", True) is not False)

    check("qwen-server остаётся серверным CDP", agents["qwen-server"].get("transport") == "cdp"
          and agents["qwen-server"].get("host") == "server")
    check("qwen-code не удалён из реестра", "qwen-code" in agents)

    main_py = (ROOT / "hub" / "app" / "main.py").read_text(encoding="utf-8")
    check("хаб отдаёт флаг enabled", 'data["enabled"] = bool(item.get("enabled", True))' in main_py)

    app_js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("селект запуска пропускает выключенных", "if (a.enabled === false)" in app_js)
    check("агент по умолчанию только включённый", "const pickable = agents.filter(a => a.enabled !== false)" in app_js)
    check("редактор роли фильтрует выключенных",
          "agents.filter(a=>a.enabled!==false).map(a=>({id:a.id,name:a.name" in app_js)
    check("выключенный агент подписан в списке", "'выключен'" in app_js)

    print(("FAILED: %d" % len(FAILS)) if FAILS else "ALL OK")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
