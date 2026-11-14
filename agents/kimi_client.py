"""Клиент Kimi Code через локальный HTTP API приложения.

Kimi Code Desktop поднимает локальный сервер (`~/.kimi-code/server/instances/*.json`)
с Bearer-токеном (`~/.kimi-code/server.token`). Сессии создаются по API, но
текст ответа и расход токенов сервер не отдаёт в /messages — они пишутся в
журнал сессии `sessions/<workspace>/<session>/agents/main/wire.jsonl`.
Поэтому ответ читается из журнала, а не из UI и не через окно.
"""

from __future__ import annotations

import glob
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

KIMI_HOME = Path(os.environ.get("KIMI_CODE_HOME") or (Path.home() / ".kimi-code"))
DEFAULT_MODEL = "kimi-code/kimi-for-coding"
EFFORTS = ("low", "high", "max")


def _token() -> str:
    env = os.environ.get("KIMI_CODE_TOKEN", "").strip()
    if env:
        return env
    path = KIMI_HOME / "server.token"
    return path.read_text(encoding="utf-8").strip()


def _base_url() -> str:
    for item in glob.glob(str(KIMI_HOME / "server" / "instances" / "*.json")):
        try:
            data = json.loads(Path(item).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("host") and data.get("port"):
            return f"http://{data['host']}:{data['port']}"
    return "http://127.0.0.1:60725"


class KimiError(RuntimeError):
    pass


class KimiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base = (base_url or _base_url()).rstrip("/")
        self.token = token or _token()

    def _call(self, path: str, payload: dict | None = None, timeout: int = 60) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8") or "{}")
            raise KimiError(f"Kimi API {path}: {body.get('msg') or exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise KimiError(f"Kimi Code недоступен: {exc}") from exc
        if body.get("code") not in (0, None):
            raise KimiError(f"Kimi API {path}: {body.get('msg')}")
        return body.get("data") or {}

    def run(self, prompt: str, *, model: str | None = None, effort: str | None = None,
            cwd: str | None = None, timeout: int = 600) -> dict:
        started = time.time()
        model = (model or DEFAULT_MODEL).strip()
        effort = (effort or "").strip().lower()
        if effort and effort not in EFFORTS:
            raise KimiError(f"Kimi не поддерживает уровень рассуждения '{effort}'")
        workdir = str(cwd or Path.cwd())
        session = self._call("/api/v1/sessions", {
            "title": "AI Hub · " + time.strftime("%H:%M:%S"),
            "metadata": {"cwd": workdir},
        })
        session_id = session.get("id")
        workspace_id = session.get("workspace_id")
        if not session_id:
            raise KimiError("Kimi не вернул идентификатор сессии")
        config: dict = {"model": model, "permission_mode": "auto"}
        if effort:
            config["thinking"] = effort
        self._call(f"/api/v1/sessions/{session_id}/profile", {"agent_config": config})
        self._call(f"/api/v1/sessions/{session_id}/prompts",
                   {"content": [{"type": "text", "text": prompt}]}, timeout=120)

        deadline = started + max(30, timeout)
        reason = ""
        while time.time() < deadline:
            time.sleep(2)
            state = self._call(f"/api/v1/sessions/{session_id}")
            reason = str(state.get("last_turn_reason") or "")
            if not state.get("busy") and reason not in ("", "running"):
                break
        if reason in ("", "running"):
            raise KimiError(f"Kimi не ответил за {timeout} с")

        wire = KIMI_HOME / "sessions" / str(workspace_id) / session_id / "agents" / "main" / "wire.jsonl"
        text, usage = _read_wire(wire, started)
        if reason == "failed" and not text:
            raise KimiError("Kimi завершил ход с ошибкой и без ответа")
        if not text:
            raise KimiError("Kimi вернул пустой ответ")
        try:
            self._call(f"/api/v1/sessions/{session_id}:archive", {})
        except KimiError:
            pass
        return {"output": text, "usage": usage, "exit_code": 0,
                "elapsed": round(time.time() - started, 2)}


def _read_wire(path: Path, since: float) -> tuple[str, dict]:
    """Достаёт текст ответа и расход токенов последнего хода из журнала сессии."""
    if not path.exists():
        return "", {}
    texts: list[str] = []
    usage: dict = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (item.get("time") or 0) < since * 1000 - 1000:
            continue
        kind = item.get("type")
        if kind == "usage.record" and isinstance(item.get("usage"), dict):
            raw = item["usage"]
            usage = {
                "input_tokens": int(raw.get("inputOther") or 0) + int(raw.get("inputCacheRead") or 0)
                + int(raw.get("inputCacheCreation") or 0),
                "output_tokens": int(raw.get("output") or 0),
                "cache_read_tokens": int(raw.get("inputCacheRead") or 0),
            }
        elif kind == "context.append_loop_event":
            event = item.get("event") or {}
            if event.get("type") != "content.part":
                continue
            part = event.get("part") or {}
            if part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
    return "\n".join(texts).strip(), usage
