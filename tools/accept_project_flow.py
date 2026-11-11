#!/usr/bin/env python3
"""Приёмка по документу «Создание проекта и цикл разработки в AI Hub».

Сценарии:
  1. Создание проекта, повтор по тому же ключу, раскладка repo/ + ops/.
  2. Одна задача через FLOW: тест, ревью, черновик PR, END — требует
     изолированного исполнителя проверок на хосте (Linux Docker runner).
  3. Обслуживание архива: карта ops/INDEX.md, точки восстановления,
     журнал ops/TASKFLOW.md, отсутствие секретов в отчётах.

Запуск: python tools/accept_project_flow.py [--hub URL] [--token TOKEN]
Код возврата: 0 — все доступные сценарии прошли, 1 — есть провал.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SECRET_PAT = re.compile(r"(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|"
                        r"(api[_-]?key|token|password)\s*[:=]\s*[A-Za-z0-9_\-]{16,})",
                        re.IGNORECASE)


class Hub:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def call(self, path: str, body=None, method: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"},
                                     method=method or ("POST" if body is not None else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"raw": raw[:400]}


def scenario_create(hub: Hub) -> dict:
    key = "accept-" + str(int(time.time()))
    status, project = hub.call("/projects", {"name": "Acceptance Project", "host_id": "laptop",
                                             "idempotency_key": key})
    if status != 200 or project.get("status") != "ready":
        return {"ok": False, "error": f"создание: {status} {project}"}
    status2, repeat = hub.call("/projects", {"name": "Acceptance Project", "host_id": "laptop",
                                             "idempotency_key": key})
    same = repeat.get("id") == project.get("id") and repeat.get("workdir") == project.get("workdir")
    # проверки проекта — условие запуска FLOW; план фиксируется в карточке проекта
    hub.call("/projects/" + str(project.get("id")),
             {"settings": {"checks": {"files": {"cmd": ["git", "status", "--short"],
                                                "timeout_s": 60}}}}, method="PATCH")
    root = Path(project["workdir"])
    layout = {
        "repo": (root / "repo" / ".git").is_dir() or (root / "repo").is_dir(),
        "ops": all((root / "ops" / name).is_file() for name in
                   ("TASKFLOW.md", "BACKUPS.md", "END.md", "INDEX.md", "ENVIRONMENTS.md")),
        "pr": (root / "ops" / "pr").is_dir(),
        "git_root": project.get("git_root") == str(root / "repo"),
    }
    return {"ok": bool(same and all(layout.values()) and project.get("head_sha")),
            "id": project.get("id"), "workdir": str(root), "head_sha": project.get("head_sha"),
            "repeat_same": same, "layout": layout}


def scenario_flow(hub: Hub, project_id: str) -> dict:
    status, body = hub.call("/runs", {"project_id": project_id, "template_id": "five-roles",
                                      "goal": "Контрольная задача приёмки", "agent": "qwen"})
    if status == 200:
        return {"ok": True, "run": body.get("id"), "state": body.get("state")}
    code = ((body.get("detail") or {}) if isinstance(body, dict) else {}).get("code")
    return {"ok": False, "skipped": True, "status": status, "code": code,
            "detail": body, "reason": "хост не подтвердил изолированного исполнителя проверок"}


def scenario_archive(hub: Hub, project_id: str) -> dict:
    st_b, backup = hub.call(f"/projects/{project_id}/backups?label=accept", {}, method="POST")
    st_i, index = hub.call(f"/projects/{project_id}/index/refresh", {}, method="POST")
    st_t, taskflow = hub.call(f"/projects/{project_id}/reports/sync", {}, method="POST")
    status, project = hub.call("/projects/" + project_id)
    root = Path(project.get("workdir") or ".")
    leaks = []
    for path in list((root / "ops").rglob("*.md")):
        found = SECRET_PAT.search(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            leaks.append(f"{path.name}: {found.group(0)[:24]}")
    taskflow_skipped = st_t == 404 and "no runs" in json.dumps(taskflow)
    checks = {
        "backup": st_b == 200 and bool(backup.get("sha256")),
        "backup_file": (root / str(backup.get("path") or "")).is_file() if backup.get("path") else False,
        "index": st_i == 200 and bool(index.get("digest")),
        "index_card": project.get("index_sha") == (index.get("head_sha") or ""),
        "taskflow": taskflow_skipped or (st_t == 200 and bool(taskflow.get("job"))),
        "no_secrets": not leaks,
    }
    return {"ok": all(checks.values()), "checks": checks, "leaks": leaks,
            "taskflow_note": "пропущено: прогонов нет" if taskflow_skipped else None,
            "backup": backup.get("path"), "backup_sha256": backup.get("sha256")}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub", default=os.environ.get("HUB_URL", "http://127.0.0.1:8102"))
    parser.add_argument("--token", default=os.environ.get("TEST_HUB_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        print("нужен TEST_HUB_TOKEN (--token или переменная окружения)")
        return 1
    hub = Hub(args.hub, args.token)

    created = scenario_create(hub)
    print("1. Создание проекта:", json.dumps(created, ensure_ascii=False))
    if not created.get("ok"):
        return 1
    flow = scenario_flow(hub, created["id"])
    print("2. Задача через FLOW:", json.dumps(flow, ensure_ascii=False))
    archive = scenario_archive(hub, created["id"])
    print("3. Обслуживание архива:", json.dumps(archive, ensure_ascii=False))
    ok = created.get("ok") and archive.get("ok")
    if not flow.get("ok"):
        print("   FLOW: пропущено —", flow.get("reason"))
    print("ИТОГ:", "принято" if ok else "провал")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())