#!/usr/bin/env python3
"""AI-Hub laptop agent — pull-воркер (протокол v2: аренда + heartbeat + outbox).

Цикл:
  1. GET  {HUB}/bridge/jobs?worker=<имя>  — атомарная аренда задания (приходит attempt_id и lease_seconds);
  2. heartbeat-поток продлевает аренду, пока агент работает;
  3. результат сначала пишется в outbox (локальный файл), потом отправляется
     POST {HUB}/bridge/result с повторами до подтверждения — потеря сети не теряет результат;
  4. при старте воркер досылает всё, что осталось в outbox.

Автоматического повторного запуска агента нет: если аренда истекла, хаб сам помечает попытку unknown.
"""

from __future__ import annotations

import json
import msvcrt
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bridge import AGENTS, TOKEN, log  # noqa: E402

HUB_FILE = os.path.join(HERE, "hub.txt")
OUTBOX = os.path.join(HERE, "outbox")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
WORKER = os.environ.get("COMPUTERNAME", "laptop")
os.makedirs(OUTBOX, exist_ok=True)


def hub_url() -> str:
    try:
        with open(HUB_FILE, encoding="utf-8") as fh:
            return fh.read().strip().rstrip("/")
    except OSError:
        return os.environ.get("HUB_URL", "").rstrip("/")


def http(method: str, url: str, body: dict | None = None, timeout: float = 45) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {"error": f"http {exc.code}"}
        return exc.code, payload


def outbox_path(job_id: str, attempt_id: str) -> str:
    return os.path.join(OUTBOX, f"{job_id}__{attempt_id}.json")


def deliver(path: str) -> bool:
    """Досылает результат из файла, пока хаб не подтвердит или не отвергнет попытку."""
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return True
    base = hub_url()
    for _ in range(120):
        try:
            code, resp = http("POST", f"{base}/bridge/result", payload, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log(f"deliver retry {payload.get('attempt_id')}: {exc}")
            time.sleep(5)
            continue
        if code == 200:
            # late/stale тоже означают «хаб сохранил»: результат лежит в late_results.
            if resp.get("outcome") in ("applied", "duplicate", "late", "stale"):
                try:
                    os.remove(path)
                except OSError:
                    pass
            return True
        if code in (404, 409):
            log(f"deliver drop {payload.get('attempt_id')}: {code} {resp}")
            try:
                os.remove(path)
            except OSError:
                pass
            return True
        time.sleep(5)
    return False


def drain_outbox() -> None:
    for name in sorted(os.listdir(OUTBOX)):
        if name.endswith(".json"):
            deliver(os.path.join(OUTBOX, name))


def heartbeat_loop(base: str, attempt_id: str, lease: int, stop: threading.Event) -> None:
    interval = max(20, lease // 3)
    while not stop.wait(interval):
        try:
            code, resp = http("POST", f"{base}/bridge/heartbeat", {"attempt_id": attempt_id}, timeout=30)
            if code == 200 and not resp.get("ok"):
                log(f"heartbeat rejected for {attempt_id} (lease lost)")
                return
        except Exception as exc:  # noqa: BLE001
            log(f"heartbeat error {attempt_id}: {exc}")


LOCK_PATH = os.path.join(HERE, "worker.lock")
_lock_handle = None


def acquire_single_instance() -> bool:
    """Один воркер на хост. Блокировка ОС снимается сама, когда процесс умирает, — чистить lock не нужно."""
    global _lock_handle
    try:
        _lock_handle = open(LOCK_PATH, "a+")
        _lock_handle.seek(0)
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if _lock_handle:
            _lock_handle.close()
            _lock_handle = None
        return False
    _lock_handle.seek(0)
    _lock_handle.truncate()
    _lock_handle.write(str(os.getpid()))
    _lock_handle.flush()
    return True


def main() -> int:
    if not acquire_single_instance():
        log("poller: уже работает другой экземпляр — второй не запускаю")
        print("worker already running", file=sys.stderr)
        return 3
    base = hub_url()
    if not base:
        print("hub.txt пуст — укажи адрес хаба, например http://192.168.31.95:8000", file=sys.stderr)
        return 2
    log(f"poller start hub={base} worker={WORKER} token={'set' if TOKEN else 'none'}")
    print(f"aihub agent polling {base} as {WORKER}")
    drain_outbox()

    while True:
        try:
            code, job = http("GET", f"{base}/bridge/jobs?worker={WORKER}&agents={','.join(AGENTS)}", timeout=45)
        except Exception as exc:  # noqa: BLE001
            log(f"poll error: {exc}")
            time.sleep(5)
            continue

        if code != 200:
            time.sleep(POLL_INTERVAL)
            continue

        job_id = job.get("id")
        attempt_id = job.get("attempt_id")
        agent_id = job.get("agent")
        prompt = job.get("prompt")
        if not job_id or not attempt_id or not agent_id or not prompt:
            time.sleep(POLL_INTERVAL)
            continue

        spec = AGENTS.get(agent_id)
        if spec is None:
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                       "error": f"unknown agent '{agent_id}'"}
            path = outbox_path(job_id, attempt_id)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_path, path)
            deliver(path)
            continue

        lease = int(job.get("lease_seconds") or 300)
        timeout = int(job.get("timeout") or spec["timeout"])
        requested_model = job.get("requested_model")
        stop = threading.Event()
        threading.Thread(target=heartbeat_loop, args=(base, attempt_id, lease, stop), daemon=True).start()

        log(f"job {job_id} attempt={attempt_id} agent={agent_id} timeout={timeout} model={requested_model}")
        started = time.time()
        try:
            # Выбор модели передаётся только адаптерам, которые его применяют.
            if requested_model and agent_id in ("codex",):
                result = spec["runner"](prompt, timeout, requested_model)
            else:
                result = spec["runner"](prompt, timeout)
            ok = result.get("exit_code") == 0
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": ok, **result}
            # Фактическая модель: если инструмент её раскрывает — сохраняем, иначе null.
            if result.get("model") is None:
                payload["model"] = requested_model
            if not ok and not payload.get("error"):
                code = result.get("exit_code")
                tail = (result.get("output") or "").strip()[-500:]
                if code in (-1, 4294967295):
                    payload["error"] = "процесс агента прерван (kill)" + (": " + tail if tail else "")
                else:
                    payload["error"] = tail or f"процесс агента завершился с кодом {code}"
        except subprocess.TimeoutExpired:
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                       "error": f"timeout after {timeout}s"}
        except Exception as exc:  # noqa: BLE001
            log("runner error " + traceback.format_exc())
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": False, "error": str(exc)}
        finally:
            stop.set()

        payload["elapsed_total"] = round(time.time() - started, 2)
        path = outbox_path(job_id, attempt_id)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_path, path)
        deliver(path)
        drain_outbox()
        time.sleep(0.5)


if __name__ == "__main__":
    raise SystemExit(main())
