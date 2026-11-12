"""Workspace-only процесс воркера AI-Hub.

Отдельная очередь для коротких операций над рабочим каталогом: длинное
задание роли (вызов модели на минуты) больше не задерживает индекс, векторы
и сверку blob'ов. Аренда, heartbeat и fencing — те же, что у основного
процесса; отличается только набор видов заданий.

Имя процесса отдельное (COMPUTERNAME-ws), чтобы в реестре воркеров было
видно, кто чем занят.
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
    "AIHUB_WORKER_KINDS": "workspace",
    "AIHUB_WORKER_NAME": f"{os.environ.get('COMPUTERNAME', 'laptop')}-ws",
    "AIHUB_ISOLATED_RUNNER_ARGV": json.dumps([
        r"C:\Windows\System32\wsl.exe", "-d", "AIHub", "--",
        "python3", "/home/hub/aihub/tools/docker_check_runner.py",
        "--config", "/home/hub/aihub/runner.json",
    ]),
    "PYTHONUNBUFFERED": "1",
})

os.chdir(HERE)
runpy.run_path(str(HERE / "worker_v2.py"), run_name="__main__")
