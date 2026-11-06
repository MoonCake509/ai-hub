"""Одна команда: останавливает воркеры, гоняет контрактные тесты на сервере, поднимает воркеры.

Использование (с ноутбука):  python tests/run_contract_tests.py

Тесты пишут в рабочую БД тестовые задания (префикс CT-), поэтому воркеры на время
прогона останавливаются — иначе они перехватывают задания раньше теста.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SSH = ["ssh", "-o", "BatchMode=yes",
       "-o", "UserKnownHostsFile=C:/Users/lavy/.ssh/known_hosts_lan",
       "-o", "ConnectTimeout=25", "-i", os.path.expanduser("~/.ssh/cubeworld_server"),
       "User@192.168.31.95"]


def laptop_worker_pids() -> list[int]:
    out = subprocess.run(["wmic", "process", "where", "name='pythonw.exe'",
                          "get", "ProcessId,CommandLine", "/format:list"],
                         capture_output=True).stdout.decode("cp866", "replace")
    return [int(m) for b in out.split("\n\n") if "worker_v2.py" in b
            for m in re.findall(r"ProcessId=(\d+)", b)]


def main() -> int:
    stopped = laptop_worker_pids()
    for pid in stopped:
        subprocess.run(["taskkill", "/f", "/pid", str(pid)], capture_output=True)
    subprocess.run(SSH + ["schtasks /end /tn AIHub-Worker"], capture_output=True)
    print(f"воркеры остановлены (ноутбук: {stopped})")
    time.sleep(1)
    try:
        with open(os.path.join(HERE, "contract_tests.py"), "rb") as fh:
            r = subprocess.run(SSH + ["python -u -X utf8 -"], stdin=fh,
                               capture_output=True, timeout=600)
        sys.stdout.write(r.stdout.decode("utf-8", "replace"))
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[-500:])
        return r.returncode
    finally:
        subprocess.run(SSH + ["schtasks /run /tn AIHub-Worker"], capture_output=True)
        subprocess.run(["wscript", r"C:\Users\lavy\aihub-bridge\start-worker.vbs"], capture_output=True)
        print("воркеры запущены снова")


if __name__ == "__main__":
    sys.exit(main())