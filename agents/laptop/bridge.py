#!/usr/bin/env python3
"""AI-Hub laptop bridge.

Тонкий HTTP-слой над локальными агентами ноутбука (Codex CLI, Qwen Desktop).
Нужен потому, что Codex на сервере не авторизован, а Qwen живёт только тут.

Эндпоинты:
  GET  /health                     — живость (без токена)
  GET  /agents                     — список агентов и их доступность
  POST /run                        — {"agent": "codex", "prompt": "...", "timeout": 600}

Авторизация: Authorization: Bearer <token из token.txt>.
"""

from __future__ import annotations

import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.txt")
LOG_FILE = os.path.join(HERE, "bridge.log")
PORT = int(os.environ.get("BRIDGE_PORT", "8010"))

CODEX_EXE = os.environ.get(
    "CODEX_EXE",
    r"C:\Users\lavy\AppData\Local\OpenAI\Codex\bin\a159911ff70742e9\codex.exe",
)
QWEN_JS = os.environ.get("QWEN_JS", r"C:\Users\lavy\tools\qwen.js")
OPENCLAW_CMD = os.environ.get("OPENCLAW_CMD") or shutil.which("openclaw") or r"C:\Users\lavy\AppData\Roaming\npm\openclaw.cmd"
HERMES_EXE = os.environ.get("HERMES_EXE") or shutil.which("hermes") or r"C:\Users\lavy\AppData\Local\hermes\bin\hermes.exe"
COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
NODE_EXE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"

CREATE_NO_WINDOW = 0x08000000
LOCKS = {name: threading.Lock() for name in ("codex", "qwen", "openclaw", "hermes")}


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def read_token() -> str:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return os.environ.get("BRIDGE_TOKEN", "")


TOKEN = read_token()


def parse_codex(stdout: str) -> dict:
    """Разбирает JSONL от `codex exec --json`."""
    text_parts: list[str] = []
    usage: dict = {}
    thread_id = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        if etype == "thread.started":
            thread_id = evt.get("thread_id")
        elif etype == "item.completed":
            item = evt.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                text_parts.append(item["text"])
        elif etype == "turn.completed":
            usage = evt.get("usage") or {}
    return {
        "output": "\n".join(text_parts).strip(),
        "usage": usage,
        "thread_id": thread_id,
        "raw_lines": len(stdout.splitlines()),
    }


def run_codex(prompt: str, timeout: int) -> dict:
    cmd = [CODEX_EXE, "exec", "--json", "--skip-git-repo-check", prompt]
    started = time.time()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    parsed = parse_codex(out)
    if not parsed["output"] and err.strip():
        parsed["output"] = err.strip()[-2000:]
    parsed["exit_code"] = proc.returncode
    parsed["elapsed"] = round(time.time() - started, 2)
    return parsed


def run_qwen(prompt: str, timeout: int) -> dict:
    cmd = [NODE_EXE, QWEN_JS, "ask", prompt]
    started = time.time()
    proc = subprocess.run(
        cmd,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
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


def _deep_text(obj, key_names, depth=0):
    """Первое непустое строковое значение по списку ключей на любой глубине."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key in key_names:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in obj.values():
            found = _deep_text(val, key_names, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for val in obj:
            found = _deep_text(val, key_names, depth + 1)
            if found:
                return found
    return None


def run_openclaw(prompt, timeout):
    """OpenClaw CLI. Промпт уходит файлом (--message-file): без проблем с кавычками и длиной."""
    started = time.time()
    fd, msg_path = tempfile.mkstemp(prefix="aihub-oc-", suffix=".txt", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    try:
        cmd = [COMSPEC, "/c", OPENCLAW_CMD, "agent", "--message-file", msg_path, "--json"]
        proc = subprocess.run(
            cmd, capture_output=True, stdin=subprocess.DEVNULL,
            timeout=timeout, creationflags=CREATE_NO_WINDOW,
        )
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
    return {
        "output": text,
        "usage": usage,
        "exit_code": proc.returncode,
        "elapsed": round(time.time() - started, 2),
    }


def run_hermes(prompt, timeout):
    """Hermes Agent, режим одного запроса: hermes -z '<prompt>'."""
    started = time.time()
    proc = subprocess.run(
        [HERMES_EXE, "-z", prompt], capture_output=True, stdin=subprocess.DEVNULL,
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )
    out = ANSI_RE.sub("", proc.stdout.decode("utf-8", "replace")).strip()
    err = ANSI_RE.sub("", proc.stderr.decode("utf-8", "replace")).strip()
    return {
        "output": out or err[-2000:],
        "usage": {},
        "exit_code": proc.returncode,
        "elapsed": round(time.time() - started, 2),
    }


AGENTS = {
    "codex": {
        "name": "Codex CLI (ноутбук)",
        "runner": run_codex,
        "path": CODEX_EXE,
        "timeout": 900,
    },
    "qwen": {
        "name": "Qwen Desktop (ноутбук, CDP)",
        "runner": run_qwen,
        "path": QWEN_JS,
        "timeout": 600,
    },
    "openclaw": {
        "name": "OpenClaw (ноутбук, CLI)",
        "runner": run_openclaw,
        "path": OPENCLAW_CMD,
        "timeout": 600,
    },
    "hermes": {
        "name": "Hermes Agent (ноутбук, CLI)",
        "runner": run_hermes,
        "path": HERMES_EXE,
        "timeout": 600,
    },
}


class Handler(BaseHTTPRequestHandler):
    server_version = "aihub-bridge/0.1"

    def log_message(self, fmt, *args):  # noqa: A003
        log("http " + (fmt % args))

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"status": "ok", "service": "aihub-bridge", "host": os.environ.get("COMPUTERNAME", "")})
            return
        if self.path == "/agents":
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            items = []
            for name, spec in AGENTS.items():
                items.append({
                    "id": name,
                    "name": spec["name"],
                    "available": bool(spec["path"]) and os.path.exists(spec["path"]),
                    "timeout": spec["timeout"],
                })
            self._send(200, {"count": len(items), "agents": items})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/run":
            self._send(404, {"error": "not found"})
            return
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(400, {"error": f"bad json: {exc}"})
            return

        agent_id = (payload.get("agent") or "").strip()
        prompt = (payload.get("prompt") or "").strip()
        spec = AGENTS.get(agent_id)
        if spec is None:
            self._send(404, {"error": f"unknown agent '{agent_id}'", "known": list(AGENTS)})
            return
        if not prompt:
            self._send(400, {"error": "prompt is required"})
            return
        if not os.path.exists(spec["path"]):
            self._send(503, {"error": f"agent binary not found: {spec['path']}"})
            return

        timeout = int(payload.get("timeout") or spec["timeout"])
        lock = LOCKS.setdefault(agent_id, threading.Lock())
        if not lock.acquire(blocking=False):
            self._send(429, {"error": f"agent '{agent_id}' busy"})
            return
        try:
            log(f"run {agent_id} prompt_len={len(prompt)} timeout={timeout}")
            result = spec["runner"](prompt, timeout)
            log(f"done {agent_id} exit={result.get('exit_code')} out_len={len(result.get('output') or '')} elapsed={result.get('elapsed')}")
            self._send(200, {"ok": result.get("exit_code") == 0, "agent": agent_id, **result})
        except subprocess.TimeoutExpired:
            log(f"timeout {agent_id} after {timeout}s")
            self._send(504, {"ok": False, "agent": agent_id, "error": f"timeout after {timeout}s"})
        except Exception as exc:  # noqa: BLE001
            log("error " + traceback.format_exc())
            self._send(500, {"ok": False, "agent": agent_id, "error": str(exc)})
        finally:
            lock.release()


def main() -> int:
    if not TOKEN:
        print("WARNING: token.txt пуст — сервис без авторизации", file=sys.stderr)
    log(f"bridge start port={PORT} token={'set' if TOKEN else 'none'}")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"aihub-bridge listening on 0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
