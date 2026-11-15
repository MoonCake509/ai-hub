"""Запуск рабочего воркера AI-Hub с корректным env (без шелла).

JSON argv раннера собирается здесь, поэтому обратные слэши Windows-путей
не искажаются оболочкой. Токен берётся из stand.env стенда.
"""
from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent
STAND = Path(r"R:\AI-Hub\workspace\flow-finish-0443c2a4\stand.env")

token = ""
if STAND.is_file():
    for line in STAND.read_text(encoding="utf-8").splitlines():
        if line.startswith("TEST_HUB_TOKEN="):
            token = line.split("=", 1)[1].strip()

os.environ.update({
    "HUB_URL": "http://127.0.0.1:8102",
    "HUB_TOKEN": token,
    "BRIDGE_TOKEN": token,
    "AIHUB_WORKSPACE_DIR": r"C:\Users\user\AppData\Local\aihub\workspaces",
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
