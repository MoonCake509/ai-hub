"""Одна команда: поднимает изолированный стенд, гоняет контрактные тесты, убирает за собой.

    python tests/run_contract_tests.py

Живые воркеры НЕ останавливаются. Рабочая БД и очередь не изменяются:
стенд — отдельный контейнер на 127.0.0.1:8100 со схемой aihub_test.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WSL = r"C:\Program Files\WSL\wsl.exe"


def wsl(cmd: str, timeout: int = 300) -> tuple[int, str]:
    r = subprocess.run([WSL, "-d", "AIHub", "-e", "bash", "-lc", cmd],
                       capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")


def main() -> int:
    print("== поднимаю стенд (aihub-hub-test, 127.0.0.1:8100, схема aihub_test)")
    code, out = wsl("cd /mnt/r/AI-Hub/infra && docker compose -f docker-compose.test.yml "
                    "--env-file .env up -d 2>&1 | tail -5")
    print(out.strip())
    if code != 0:
        return 1

    try:
        for _ in range(40):
            code, out = wsl("curl -s -m 3 http://127.0.0.1:8100/health", timeout=30)
            if '"db":true' in out:
                print("стенд готов:", out.strip())
                break
            time.sleep(2)
        else:
            print("стенд не поднялся:", out[-500:])
            return 1

        print("== контрактные тесты")
        with open(os.path.join(HERE, "contract_tests.py"), "rb") as fh:
            r = subprocess.run(
                ["ssh", "-o", "BatchMode=yes",
                 "-o", "UserKnownHostsFile=C:/Users/lavy/.ssh/known_hosts_lan",
                 "-o", "ConnectTimeout=25", "-i", os.path.expanduser("~/.ssh/cubeworld_server"),
                 "User@192.168.31.95", "python -u -X utf8 -"],
                stdin=fh, capture_output=True, timeout=600)
        sys.stdout.write(r.stdout.decode("utf-8", "replace"))
        sys.stderr.write(r.stderr.decode("utf-8", "replace")[-500:])
        return r.returncode
    finally:
        print("== убираю стенд и тестовую схему")
        _, out = wsl("cd /mnt/r/AI-Hub/infra && docker compose -f docker-compose.test.yml "
                     "--env-file .env down 2>&1 | tail -3; "
                     "docker exec aihub-postgres psql -U llmproxy -d litellm "
                     "-c 'drop schema if exists aihub_test cascade'")
        print(out.strip())


if __name__ == "__main__":
    sys.exit(main())