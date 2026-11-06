#!/usr/bin/env python3
"""AI-Hub worker на серверном хосте (pull-протокол v2).

Обслуживает серверных агентов (Qwen Desktop через CDP). Работает вне Docker:
GUI-приложение в контейнере не запустить. Ходит на хаб через 127.0.0.1:8000.
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
LOG = os.path.join(HERE, "worker.log")
OUTBOX = os.path.join(HERE, "outbox")
CREATE_NO_WINDOW = 0x08000000
NODE = r"C:\Program Files\nodejs\node.exe"
QWEN_JS = r"C:\Users\user\bin\qwen.js"
os.makedirs(OUTBOX, exist_ok=True)


def log(msg: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def read(name: str, default: str = "") -> str:
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return default


HUB = os.environ.get("HUB_URL") or read("hub.txt", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("HUB_TOKEN") or read("token.txt", "")
WORKER = os.environ.get("COMPUTERNAME", "server") + "-host"


def run_qwen(prompt: str, timeout: int) -> dict:
    env = dict(os.environ)
    env["QWEN_CDP_PORT"] = os.environ.get("QWEN_CDP_PORT", "9333")
    started = time.time()
    proc = subprocess.run(
        [NODE, QWEN_JS, "ask", prompt],
        capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
        env=env, creationflags=CREATE_NO_WINDOW,
    )
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    return {
        "output": out or err[-2000:],
        "usage": {},
        "exit_code": proc.returncode,
        "elapsed": round(time.time() - started, 2),
    }


AGENTS = {"qwen-server": {"runner": run_qwen, "timeout": 600}}


def http(method: str, path: str, body: dict | None = None, timeout: float = 45) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(HUB + path, data=data, method=method)
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
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return True
    for _ in range(120):
        try:
            code, resp = http("POST", "/bridge/result", payload, timeout=60)
        except Exception as exc:  # noqa: BLE001
            log(f"deliver retry {payload.get('attempt_id')}: {exc}")
            time.sleep(5)
            continue
        if code == 200:
            if resp.get("outcome") in ("applied", "duplicate"):
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


def heartbeat_loop(attempt_id: str, lease: int, stop: threading.Event) -> None:
    interval = max(20, lease // 3)
    while not stop.wait(interval):
        try:
            code, resp = http("POST", "/bridge/heartbeat", {"attempt_id": attempt_id}, timeout=30)
            if code == 200 and not resp.get("ok"):
                log(f"heartbeat rejected {attempt_id}")
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
        log("worker: уже работает другой экземпляр — второй не запускаю")
        print("worker already running", file=sys.stderr)
        return 3
    log(f"worker start hub={HUB} worker={WORKER} token={'set' if TOKEN else 'none'}")
    print(f"aihub-server-worker polling {HUB} as {WORKER}")
    drain_outbox()
    while True:
        try:
            code, job = http("GET", f"/bridge/jobs?worker={WORKER}&agents={','.join(AGENTS)}")
        except Exception as exc:  # noqa: BLE001
            log(f"poll error: {exc}")
            time.sleep(5)
            continue
        if code != 200:
            time.sleep(3)
            continue

        job_id = job.get("id")
        attempt_id = job.get("attempt_id")
        agent_id = job.get("agent")
        prompt = job.get("prompt")
        if not job_id or not attempt_id or not agent_id or not prompt:
            time.sleep(2)
            continue

        spec = AGENTS.get(agent_id)
        if spec is None:
            path = outbox_path(job_id, attempt_id)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"job_id": job_id, "attempt_id": attempt_id, "ok": False,
                           "error": f"agent '{agent_id}' is not served by this worker"}, fh)
            deliver(path)
            continue

        lease = int(job.get("lease_seconds") or 300)
        timeout = int(job.get("timeout") or spec["timeout"])
        stop = threading.Event()
        threading.Thread(target=heartbeat_loop, args=(attempt_id, lease, stop), daemon=True).start()

        log(f"job {job_id} attempt={attempt_id} agent={agent_id} timeout={timeout}")
        started = time.time()
        try:
            result = spec["runner"](prompt, timeout)
            ok = result.get("exit_code") == 0
            payload = {"job_id": job_id, "attempt_id": attempt_id, "ok": ok, **result}
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
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        deliver(path)
        drain_outbox()
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
