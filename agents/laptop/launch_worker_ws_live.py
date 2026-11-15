"""Workspace-воркер для БОЕВОГО хаба (порт 8090).

Отдельная очередь коротких операций над каталогом проекта: подготовка проекта,
индекс, сверка файлов. Токен берётся из infra/.env, наружу ничего не печатается.
"""
from __future__ import annotations

import json
import os
import re
import runpy
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = Path(r"R:\AI-Hub\infra\.env")

token = ""
if ENV.is_file():
    match = re.search(r"^HUB_TOKEN=(.+)$", ENV.read_text(encoding="utf-8"), re.M)
    if match:
        token = match.group(1).strip().strip('"').strip("'")

if not token:
    raise SystemExit("HUB_TOKEN не найден в infra/.env")

os.environ.update({
    "HUB_URL": "http://127.0.0.1:8090",
    "HUB_TOKEN": token,
    "BRIDGE_TOKEN": token,
    # Ярлык хаба, а не имя машины: workspace-задания арендуются по host_id
    # проекта (server), и capabilities/registry тоже индексируются по нему.
    # Без этого воркер опрашивает host=WIN-V76QC2G60Q1 и server-задания
    # остаются в очереди (регресс af0e4d7).
    "AIHUB_HOST_ID": "server",
    "AIHUB_WORKSPACE_DIR": r"C:\Users\user\AppData\Local\aihub\workspaces",
    "QWEN_JS": r"C:\Users\user\bin\qwen.js",
    "AIHUB_LOCAL_LLM": "http://127.0.0.1:11434",
    "AIHUB_WORKER_KINDS": "workspace",
    "AIHUB_WORKER_NAME": f"{os.environ.get('COMPUTERNAME', 'laptop')}-live-ws",
    "AIHUB_ISOLATED_RUNNER_ARGV": json.dumps([
        r"C:\Windows\System32\wsl.exe", "-d", "AIHub", "--",
        "python3", "/home/hub/aihub/tools/docker_check_runner.py",
        "--config", "/home/hub/aihub/runner.json",
    ]),
    "PYTHONUNBUFFERED": "1",
})

os.chdir(HERE)
runpy.run_path(str(HERE / "worker_v2.py"), run_name="__main__")
