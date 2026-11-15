"""Воркер для самостоятельного экземпляра хаба на 8090 (проверка вручную).

Отдельное имя и отдельный каталог рабочих копий: не конфликтует с воркером
стенда 8102 (у того своё имя и свой lock).
"""
from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = Path(r"R:\AI-Hub\infra\.env")

token = ""
if ENV.is_file():
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("HUB_TOKEN="):
            token = line.split("=", 1)[1].strip()

# Ключи провайдеров для CLI-агентов (Codex/Qwen) берём из окружения Hermes:
# воркер не должен зависеть от того, что кто-то вручную выставил их в сессии.
_HERMES_ENV = Path(os.path.expanduser(r"~\AppData\Local\hermes\.env"))
if _HERMES_ENV.is_file():
    for _line in _HERMES_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
        if _line.startswith(("ROUTER_CHEAP_API_KEY=", "PISHNYE_AI_RU_API_KEY=", "CLAUDEHUB_API_KEY=")):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"'))

os.environ.update({
    "HUB_URL": "http://127.0.0.1:8090",
    "HUB_TOKEN": token,
    "BRIDGE_TOKEN": token,
    "AIHUB_WORKER_NAME": "WIN-V76QC2G60Q1-chk",
    "AIHUB_WORKER_KINDS": "agent",
    "AIHUB_WORKSPACE_DIR": r"C:\Users\user\AppData\Local\aihub\workspaces-check",
    "QWEN_JS": r"C:\Users\user\bin\qwen.js",
    "AIHUB_LOCAL_LLM": "http://127.0.0.1:11434",
    "AIHUB_ISOLATED_RUNNER_ARGV": json.dumps([
        r"C:\Windows\System32\wsl.exe", "-d", "AIHub", "--",
        "python3", "/home/hub/aihub/tools/docker_check_runner.py",
        "--config", "/home/hub/aihub/runner.json",
    ]),
    "PYTHONUNBUFFERED": "1",
})

os.chdir(HERE)
runpy.run_path(str(HERE / "worker_v2.py"), run_name="__main__")
