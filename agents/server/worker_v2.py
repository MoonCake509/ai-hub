#!/usr/bin/env python3
"""AI-Hub worker на серверном хосте (pull-протокол v2).

Обслуживает серверных агентов (Qwen Desktop через CDP). Работает вне Docker:
GUI-приложение в контейнере не запустить. Ходит на хаб через 127.0.0.1:8000.
"""

from __future__ import annotations

import json
import msvcrt
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.abspath(os.environ.get("AIHUB_WORKER_HOME") or HERE)
LOG = os.path.join(STATE_DIR, "worker.log")
OUTBOX = os.path.join(STATE_DIR, "outbox")
CREATE_NO_WINDOW = 0x08000000
NODE = r"C:\Program Files\nodejs\node.exe"
QWEN_JS = r"C:\Users\user\bin\qwen.js"
OPENCLAW_CMD = os.environ.get("OPENCLAW_CMD") or r"C:\Users\user\AppData\Roaming\npm\openclaw.cmd"
HERMES_EXE = os.environ.get("HERMES_EXE") or r"C:\Users\user\AppData\Local\hermes\bin\hermes.exe"
COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
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


ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _deep_text(obj, keys, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in obj.values():
            got = _deep_text(v, keys, depth + 1)
            if got:
                return got
    elif isinstance(obj, list):
        for v in obj:
            got = _deep_text(v, keys, depth + 1)
            if got:
                return got
    return None


def run_openclaw(prompt: str, timeout: int) -> dict:
    started = time.time()
    fd, msg_path = tempfile.mkstemp(prefix="aihub-oc-", suffix=".txt", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    try:
        proc = subprocess.run([COMSPEC, "/c", OPENCLAW_CMD, "agent", "--message-file", msg_path, "--json"],
                              capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout)
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass
    out = ANSI_RE.sub("", proc.stdout.decode("utf-8", "replace")).strip()
    err = ANSI_RE.sub("", proc.stderr.decode("utf-8", "replace")).strip()
    text = ""
    usage = {}
    try:
        start = out.find("{")
        data = json.loads(out[start:] if start >= 0 else out)
        text = _deep_text(data, ("finalAssistantMessageRawText", "text")) or ""
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
    except (ValueError, json.JSONDecodeError):
        text = out
    if not text:
        text = (err or out)[-2000:]
    return {"output": text, "usage": usage, "exit_code": proc.returncode,
            "elapsed": round(time.time() - started, 2)}


def run_hermes(prompt: str, timeout: int) -> dict:
    started = time.time()
    proc = subprocess.run([HERMES_EXE, "-z", prompt], capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=timeout)
    out = ANSI_RE.sub("", proc.stdout.decode("utf-8", "replace")).strip()
    err = ANSI_RE.sub("", proc.stderr.decode("utf-8", "replace")).strip()
    return {"output": out or err[-2000:], "usage": {}, "exit_code": proc.returncode,
            "elapsed": round(time.time() - started, 2)}


SERVER_MODEL_ROUTES = {
    ("llm-server", "local", "dolphin-2.9-llama3-8b.Q4_K_M"):
        ("chat", "http://127.0.0.1:11434/v1/chat/completions"),
    ("xiaomi-ocr-server", "local", "xiaomi-ocr-q8"):
        ("ocr", "http://127.0.0.1:8787/api/ocr"),
}


def _reported_model(data: dict, usage: dict) -> str | None:
    for value in (data.get("model"), usage.get("model")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _request_local_json(url: str, body: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"local model HTTP {exc.code}") from None
    if not isinstance(result, dict):
        raise RuntimeError("local model returned a non-object response")
    return result


def run_llm(prompt: str, timeout: int, job: dict | None = None) -> dict:
    """Dispatch only declared local models to their fixed loopback services."""
    started = time.time()
    job = job or {}
    agent_id = job.get("agent")
    profile = job.get("requested_profile") or "local"
    model = job.get("requested_model")
    route = SERVER_MODEL_ROUTES.get((agent_id, profile, model))
    if route is None:
        return {"output": "", "usage": {}, "exit_code": 1,
                "error": "unsupported server agent/profile/model selection",
                "elapsed": round(time.time() - started, 2)}
    kind, url = route
    try:
        if kind == "chat":
            if any((att.get("mime") or "").startswith("image/")
                   for att in (job.get("attachments") or [])):
                raise ValueError("Dolphin is text-only and cannot receive image attachments")
            data = _request_local_json(url, {
                "model": model,
                "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
                "temperature": 0.2,
            }, timeout)
            choices = data.get("choices") or []
            content = ((choices[0].get("message") or {}).get("content") if choices else "") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                )
            text = content.strip() if isinstance(content, str) else ""
            actual_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        elif kind == "ocr":
            import base64
            attachments = job.get("attachments") or []
            images = [att for att in attachments if (att.get("mime") or "").lower() in
                      ("image/png", "image/jpeg")]
            if len(images) != 1:
                raise ValueError("Xiaomi OCR requires exactly one uploaded PNG/JPEG image")
            attachment = images[0]
            mime = (attachment.get("mime") or "").lower()
            if mime not in ("image/png", "image/jpeg"):
                raise ValueError("Xiaomi OCR only accepts PNG/JPEG image attachments")
            content_b64 = attachment.get("content_b64") or ""
            try:
                raw = base64.b64decode(content_b64, validate=True)
            except Exception:
                raise ValueError("image attachment content is invalid") from None
            if not raw or len(raw) > 2_000_000:
                raise ValueError("image attachment size is invalid")
            # OCR receives one image and the user's question. The assembled hub
            # prompt stays on the job for audit, but its role/history/attachment
            # metadata must never be sent as OCR instructions.
            task_text = job.get("task_text")
            ocr_prompt = task_text.strip() if isinstance(task_text, str) and task_text.strip() else prompt
            data = _request_local_json(url, {
                "task": "document",
                "prompt": ocr_prompt,
                "image_base64": content_b64,
                "image_mime": mime,
                "max_tokens": 4096,
            }, timeout)
            text = data.get("content") if isinstance(data.get("content"), str) else ""
            actual_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        else:
            raise ValueError("unknown server model route")
        if not text.strip():
            raise RuntimeError("local model returned an empty response")
        usage = dict(actual_usage)
        reported = _reported_model(data, usage)
        if reported:
            usage["reported_model"] = reported
        return {"output": text.strip(), "usage": usage, "exit_code": 0,
                "elapsed": round(time.time() - started, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"output": "", "usage": {}, "exit_code": 1,
                "error": str(exc)[:500], "elapsed": round(time.time() - started, 2)}

AGENTS = {
    "qwen-server": {"runner": run_qwen, "timeout": 600},
    "openclaw-server": {"runner": run_openclaw, "timeout": 600},
    "hermes-server": {"runner": run_hermes, "timeout": 600},
    "llm-server": {"runner": run_llm, "timeout": 300, "job_args": True},
    "xiaomi-ocr-server": {"runner": run_llm, "timeout": 600, "job_args": True},
}


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
    except (OSError, ValueError) as exc:
        log(f"deliver retained unreadable outbox file: {type(exc).__name__}")
        return False
    for attempt in range(3):
        try:
            code, resp = http("POST", "/bridge/result", payload, timeout=60)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log(f"deliver retained {payload.get('attempt_id')}: invalid JSON acknowledgement ({type(exc).__name__})")
            return False
        except Exception as exc:  # noqa: BLE001
            log(f"deliver transport error {payload.get('attempt_id')}: {type(exc).__name__}")
            if attempt < 2:
                time.sleep(5)
                continue
            return False
        outcome = resp.get("outcome") if isinstance(resp, dict) else None
        confirmed = (
            code == 200
            and isinstance(resp, dict)
            and resp.get("ok") is True
            and resp.get("recorded") is True
            and outcome in ("applied", "duplicate", "late", "stale")
        )
        if confirmed:
            try:
                os.remove(path)
            except OSError as exc:
                log(f"deliver stored but could not remove outbox {payload.get('attempt_id')}: {type(exc).__name__}")
                return False
            return True
        if code in (404, 409):
            log(f"deliver retained {payload.get('attempt_id')}: HTTP {code}, server did not confirm durable storage")
            return False
        if code == 200:
            ack_ok = resp.get("ok") if isinstance(resp, dict) else None
            ack_recorded = resp.get("recorded") if isinstance(resp, dict) else None
            log(f"deliver retained {payload.get('attempt_id')}: invalid acknowledgement ok={ack_ok} recorded={ack_recorded} outcome={outcome}")
            return False
        log(f"deliver retained {payload.get('attempt_id')}: HTTP {code}, server did not confirm durable storage")
        return False
    return False


def drain_outbox() -> None:
    for name in sorted(os.listdir(OUTBOX)):
        if name.endswith(".json"):
            deliver(os.path.join(OUTBOX, name))


def execute_job(spec: dict, prompt: str, timeout: int, job: dict) -> dict:
    """Run only when the leased execution context is supported by this runner."""
    if job.get("workdir") and not (spec.get("supports_workdir") and spec.get("job_args")):
        return {"output": "", "usage": {}, "exit_code": 1,
                "error": "server runner does not support the project's working directory"}
    if spec.get("job_args"):
        return spec["runner"](prompt, timeout, job)
    return spec["runner"](prompt, timeout)


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


LOCK_PATH = os.path.join(STATE_DIR, "worker.lock")
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
            result = execute_job(spec, prompt, timeout, job)
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
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp_path, path)
        deliver(path)
        drain_outbox()
        time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
