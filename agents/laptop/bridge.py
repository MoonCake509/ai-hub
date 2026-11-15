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
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "token.txt")
LOG_FILE = os.path.join(HERE, "bridge.log")
PORT = int(os.environ.get("BRIDGE_PORT", "8010"))

CODEX_EXE_DEFAULT = r"C:\Users\lavy\AppData\Local\OpenAI\Codex\bin\a159911ff70742e9\codex.exe"
CODEX_EXE = os.environ.get("CODEX_EXE", CODEX_EXE_DEFAULT)
QWEN_JS = os.environ.get("QWEN_JS", r"C:\Users\lavy\tools\qwen.js")
OPENCLAW_CMD = os.environ.get("OPENCLAW_CMD") or shutil.which("openclaw") or r"C:\Users\lavy\AppData\Roaming\npm\openclaw.cmd"
HERMES_EXE = os.environ.get("HERMES_EXE") or shutil.which("hermes") or r"C:\Users\lavy\AppData\Local\hermes\bin\hermes.exe"
COMSPEC = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
NODE_EXE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"

CREATE_NO_WINDOW = 0x08000000
LOCKS = {name: threading.Lock() for name in ("codex", "qwen", "openclaw", "hermes", "kimi")}
OPENCLAW_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh")


def resolve_codex_exe() -> str:
    """Resolve the current trusted Codex CLI, honoring an explicit override."""
    override = os.environ.get("CODEX_EXE")
    if override and override.strip():
        return override.strip()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        install_root = os.path.join(local_app_data, "OpenAI", "Codex", "bin")
        try:
            candidates = [
                os.path.join(install_root, entry, "codex.exe")
                for entry in os.listdir(install_root)
                if os.path.isfile(os.path.join(install_root, entry, "codex.exe"))
            ]
        except OSError:
            candidates = []
        if candidates:
            return max(candidates, key=os.path.getmtime)

    # Второй доверенный способ установки — npm-пакет @openai/codex: шим в
    # %APPDATA%\npm\codex.cmd запускает платформенный бинарь из vendor.
    app_data = os.environ.get("APPDATA")
    if app_data:
        vendor = os.path.join(app_data, "npm", "node_modules", "@openai",
                              "codex-win32-x64", "vendor")
        try:
            candidates = [
                os.path.join(vendor, entry, "bin", "codex.exe")
                for entry in os.listdir(vendor)
                if os.path.isfile(os.path.join(vendor, entry, "bin", "codex.exe"))
            ]
        except OSError:
            candidates = []
        if candidates:
            return max(candidates, key=os.path.getmtime)

    # Preserve a clear missing-binary error when the trusted install roots are
    # unavailable. Do not fall back to an arbitrary executable from PATH.
    return CODEX_EXE_DEFAULT


def _agent_path(agent_id: str, spec: dict) -> str:
    path = resolve_codex_exe() if agent_id == "codex" else spec["path"]
    spec["path"] = path
    return path


# Каталог Codex — единственный источник допустимых уровней рассуждения для
# конкретной модели на этой машине. `codex debug models` печатает его локально,
# без сети и без расхода токенов; кэш привязан к mtime бинаря, поэтому после
# обновления CLI каталог перечитывается сам.
_CATALOG_LOCK = threading.Lock()
_CATALOG_CACHE: dict = {"key": None, "at": 0.0, "data": None}
CATALOG_TTL_S = 600


def _parse_codex_catalog(raw: dict) -> dict:
    models = {}
    for item in raw.get("models") or []:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug:
            continue
        levels = []
        for level in item.get("supported_reasoning_levels") or []:
            effort = str((level or {}).get("effort") or "").strip() if isinstance(level, dict) else ""
            if effort and effort not in levels:
                levels.append(effort)
        models[slug] = {
            "default": str(item.get("default_reasoning_level") or "").strip(),
            "levels": levels,
        }
    return models


def codex_model_catalog(force: bool = False) -> dict:
    """Срез каталога Codex: {models: {slug: {default, levels}}, cli, error}."""
    exe = resolve_codex_exe()
    try:
        stamp = (exe, os.path.getmtime(exe))
    except OSError:
        return {"available": False, "models": {}, "cli": exe,
                "error": "Codex CLI не найден по зарегистрированному пути"}
    with _CATALOG_LOCK:
        cached = _CATALOG_CACHE
        if (not force and cached["data"] is not None and cached["key"] == stamp
                and time.time() - cached["at"] < CATALOG_TTL_S):
            return cached["data"]
        try:
            proc = subprocess.run(
                [exe, "debug", "models"],
                capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "models": {}, "cli": exe,
                    "error": f"{type(exc).__name__}: {exc}"}
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-400:]
            return {"available": False, "models": {}, "cli": exe,
                    "error": err or f"codex debug models exited {proc.returncode}"}
        try:
            models = _parse_codex_catalog(json.loads(proc.stdout.decode("utf-8", "replace")))
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "models": {}, "cli": exe,
                    "error": f"catalog parse failed: {exc}"}
        data = {"available": bool(models), "models": models, "cli": exe, "error": None}
        _CATALOG_CACHE.update(key=stamp, at=time.time(), data=data)
        return data

# These are actual per-run provider IDs from the installed Hermes config. The
# model catalog itself is generated from the installed clients' local metadata
# and contains identifiers only (never credentials or endpoint URLs).
# Hermes CLI знает провайдеров ровно по именам из config.yaml (дефис, не подчёркивание).
HERMES_PROFILE_PROVIDERS = {
    "claudehub": "claudehub",
    "router-cheap": "router-cheap",
    "router_cheap": "router-cheap",
    "pishnye": "pishnye-ai-ru",
    "pishnye-ai-ru": "pishnye-ai-ru",
}

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_SECRET_RE = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret)"
    r"(\s*[:=]\s*)([^\s,;}]+)"
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[^\s,;}]+")


def _timeout_capture(exc: subprocess.TimeoutExpired, limit: int = 2000) -> dict[str, str]:
    """Keep bounded, redacted child output when a CLI call times out."""
    def clean(value) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        value = ANSI_RE.sub("", str(value))
        value = _URL_RE.sub("[URL]", value)
        value = _SECRET_RE.sub(r"\1\2[REDACTED]", value)
        value = _BEARER_RE.sub(r"\1[REDACTED]", value)
        return value[-limit:]

    stdout = getattr(exc, "stdout", None)
    if stdout is None:
        stdout = getattr(exc, "output", None)
    return {"stdout": clean(stdout), "stderr": clean(getattr(exc, "stderr", None))}


def _timeout_result(exc: subprocess.TimeoutExpired, started: float) -> dict:
    return {
        "output": "",
        "usage": {},
        "exit_code": 124,
        "error": f"CLI timed out after {exc.timeout}s",
        "timed_out": True,
        "timeout_capture": _timeout_capture(exc),
        "elapsed": round(time.time() - started, 2),
    }


def _load_provider_catalog() -> dict:
    path = os.path.join(HERE, "provider-catalog.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


PROVIDER_CATALOG = _load_provider_catalog()


def _provider_models(client: str) -> dict[str, set[str]]:
    client_data = (PROVIDER_CATALOG.get("laptop") or {}).get(client) or {}
    models = {
        str(provider.get("id")): {
            str(model.get("id")) for model in provider.get("models", [])
            if isinstance(model, dict) and model.get("id")
        }
        for provider in client_data.get("providers", [])
        if isinstance(provider, dict) and provider.get("id")
    }
    # Каталог хранит id как `router_cheap`, а CLI принимает `router-cheap`:
    # считаем дефис и подчёркивание одним и тем же провайдером.
    for key, value in list(models.items()):
        models.setdefault(key.replace("_", "-"), value)
    return models


def _clean_cwd(cwd: str | None) -> str | None:
    if cwd is None or not str(cwd).strip():
        return None
    path = os.path.abspath(os.path.expanduser(str(cwd).strip()))
    if not os.path.isdir(path):
        raise ValueError(f"рабочая папка проекта не найдена на этом устройстве: {path}")
    return path


def _hermes_model(model: str | None, profile: str | None) -> tuple[str | None, str | None]:
    if not model and not profile:
        return None, None
    providers = _provider_models("hermes")
    if not providers:
        raise ValueError("локальный каталог Hermes не загружен; выбор профиля/модели отклонён")
    profile = (profile or "").strip()
    model = (model or "").strip()
    provider = HERMES_PROFILE_PROVIDERS.get(profile) if profile else None
    model_id = model
    if profile and provider is None:
        raise ValueError(f"Hermes provider profile '{profile}' не подтверждён установленной конфигурацией")
    if "/" in model:
        if provider and model_id in providers[provider]:
            pass  # The provider's native model ID itself contains '/'.
        else:
            model_provider, suffix = model.split("/", 1)
            if provider and model_provider == profile:
                # UI may keep the stable profile ID as the canonical prefix while
                # Hermes itself uses an underscore in the installed provider ID.
                model_provider = provider
            if provider and model_provider != provider:
                raise ValueError(f"модель '{model}' не принадлежит профилю '{profile}'")
            model_id = suffix
            provider = model_provider
    elif provider and model_id in providers.get(provider, set()):
        pass
    if provider is None and model_id:
        matches = [pid for pid, models in providers.items() if model_id in models]
        if len(matches) != 1:
            raise ValueError(f"модель '{model}' не имеет однозначного Hermes provider")
        provider = matches[0]
    if not provider or provider not in providers:
        raise ValueError(f"Hermes provider '{provider or ''}' отсутствует в локальном каталоге")
    if not model_id:
        raise ValueError("Hermes model не указана")
    if model_id not in providers[provider]:
        # Каталог может отставать от провайдера: не блокируем выбор, а пробуем
        # напрямую — если модели нет, CLI вернёт понятную ошибку сам.
        log(f"warn hermes model {model_id!r} отсутствует в каталоге {provider}, пробуем напрямую")
    return provider, model_id


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


def run_codex(prompt: str, timeout: int, requested_model: str | None = None,
              requested_profile: str | None = None, *, cwd: str | None = None,
              model: str | None = None, workdir: str | None = None,
              reasoning_effort: str | None = None) -> dict:
    # Модель применяется флагом конкретного запуска, а не правкой глобального конфига.
    if requested_profile and requested_profile != "openai":
        # Older callers passed workdir as the fourth positional argument.
        if cwd is None and os.path.isdir(requested_profile):
            cwd = requested_profile
            requested_profile = None
        else:
            raise ValueError(f"Codex profile '{requested_profile}' is not selectable by this adapter")
    cmd = [resolve_codex_exe(), "exec", "--json", "--skip-git-repo-check"]
    # model/workdir remain accepted for older callers.
    requested_model = requested_model if requested_model is not None else model
    cwd = cwd if cwd is not None else workdir
    model = requested_model
    if model:
        cmd += ["-m", model]
    # Уровень рассуждения — на один этот вызов, глобальный конфиг не трогаем.
    # Значение проверено хабом по каталогу; здесь только защита от подстановки.
    effort = (reasoning_effort or "").strip()
    if effort:
        if not re.fullmatch(r"[a-z]{2,16}", effort):
            raise ValueError(f"invalid reasoning_effort '{effort}'")
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    # Default is explicitly read-only. File writes are enabled only for a
    # caller-supplied, existing workspace path, never by widening global policy.
    try:
        cwd = _clean_cwd(cwd)
    except ValueError as exc:
        return {"output": "", "exit_code": 2, "elapsed": 0.0, "error": str(exc)}
    if cwd:
        # `--approve-for-me` keeps Codex in its workspace-write sandbox while
        # routing approval requests through its supported automatic review.
        cmd += ["--cd", cwd, "--approve-for-me"]
    else:
        cmd += ["--sandbox", "read-only"]
    # Промпт через stdin: Codex читает его как входные данные, а не как флаг.
    # Аргументы (без промпта) пишем в лог: по нему видно, что именно исполнено.
    log("codex exec args: " + " ".join(cmd[1:]))
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            input=prompt.encode("utf-8"),
            timeout=timeout,
            cwd=cwd,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(exc, started)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    parsed = parse_codex(out)
    if not parsed["output"] and err.strip():
        parsed["output"] = err.strip()[-2000:]
    parsed["exit_code"] = proc.returncode
    parsed["elapsed"] = round(time.time() - started, 2)
    return parsed


def qwen_desktop_mode(reasoning_effort: str | None) -> str:
    value = str(reasoning_effort or '').strip().lower()
    if not value:
        return 'auto'
    if value in {'low', 'fast'}:
        return 'fast'
    if value in {'medium', 'high', 'xhigh', 'max', 'ultra', 'thinking'}:
        return 'thinking'
    raise ValueError(f'unsupported Qwen Desktop reasoning level: {reasoning_effort}')


def run_qwen(prompt: str, timeout: int, requested_model: str | None = None,
             requested_profile: str | None = None, *, cwd: str | None = None,
             reasoning_effort: str | None = None) -> dict:
    mode = qwen_desktop_mode(reasoning_effort)
    cmd = [NODE_EXE, QWEN_JS, "ask-json", requested_model or "", mode, prompt]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(exc, started)
    out = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    try:
        result = json.loads(out) if out else {}
    except json.JSONDecodeError:
        result = {"output": out}
    result.setdefault("output", err[-2000:] if err else "")
    result["exit_code"] = proc.returncode
    result["elapsed"] = round(time.time() - started, 2)
    return result


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


def run_openclaw(prompt: str, timeout: int, requested_model: str | None = None,
                 requested_profile: str | None = None, reasoning_effort: str | None = None,
                 *, cwd: str | None = None) -> dict:
    """OpenClaw CLI honours --model <provider/model> and --thinking <level>."""
    if cwd and str(cwd).strip():
        return {
            "output": "", "usage": {}, "exit_code": 2, "elapsed": 0.0,
            "error": "OpenClaw cannot guarantee the requested project working directory yet",
        }
    started = time.time()
    fd, msg_path = tempfile.mkstemp(prefix="aihub-oc-", suffix=".txt", text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    try:
        # Keep the previously used `openclaw agent` path. The newer
        # `agent exec` path is experimental and is not in live dispatch.
        cmd = [COMSPEC, "/c", OPENCLAW_CMD, "agent", "--message-file", msg_path, "--json"]
        if requested_model:
            model_id = requested_model if "/" in requested_model or not requested_profile else f"{requested_profile}/{requested_model}"
            cmd += ["--model", model_id]
        effort = (reasoning_effort or "").strip().lower()
        if effort in {"off", "minimal", "low", "medium", "high", "xhigh"}:
            cmd += ["--thinking", effort]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, stdin=subprocess.DEVNULL,
                timeout=timeout, creationflags=CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            return _timeout_result(exc, started)
    finally:
        try:
            os.unlink(msg_path)
        except OSError:
            pass
    out = ANSI_RE.sub("", proc.stdout.decode("utf-8", "replace")).strip()
    err = ANSI_RE.sub("", proc.stderr.decode("utf-8", "replace")).strip()
    text = ""
    usage = {}
    data = None
    try:
        start = out.find("{")
        data = json.loads(out[start:] if start >= 0 else out)
        text = _deep_text(data, ("finalAssistantMessageRawText", "text")) or ""
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            usage = data["usage"]
            if "input" in usage and "input_tokens" not in usage:
                usage["input_tokens"] = usage["input"]
            if "output" in usage and "output_tokens" not in usage:
                usage["output_tokens"] = usage["output"]
    except (ValueError, json.JSONDecodeError):
        text = out
    exit_code = proc.returncode
    error = None
    if isinstance(data, dict) and (data.get("ok") is False or data.get("status") == "error"):
        exit_code = exit_code or 1
        error = data.get("error") or data.get("status") or "OpenClaw reported an unsuccessful run"
    if not text:
        text = (err or "").strip()[-2000:]
    return {
        "output": text,
        "usage": usage,
        "exit_code": exit_code,
        **({"error": str(error)} if error else {}),
        "elapsed": round(time.time() - started, 2),
    }


def _hermes_json_result(stdout: str) -> tuple[str, dict, str | None] | None:
    """Разбирает `--format stream-json`: текст ответа, токены, фактическая модель."""
    import json as _json
    text, usage, model = "", {}, None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = _json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "system" and event.get("model"):
            model = model or str(event["model"])
        elif kind == "result":
            text = str(event.get("text") or "")
            model = str(event.get("model") or model or "") or None
            tokens = event.get("tokens") or {}
            if isinstance(tokens, dict) and tokens:
                usage = {
                    "input_tokens": int(tokens.get("input") or 0),
                    "output_tokens": int(tokens.get("output") or 0),
                    "total_tokens": int(tokens.get("total") or 0),
                    "cache_read_tokens": int(tokens.get("cache_read") or 0),
                }
                if model:
                    usage["reported_model"] = model
    if not text and not usage:
        return None
    return text.strip(), usage, model


def run_hermes(prompt: str, timeout: int, requested_model: str | None = None,
               requested_profile: str | None = None, *, cwd: str | None = None) -> dict:
    """Hermes one-shot with per-invocation provider/model; prompt goes over stdin.

    Вывод снимается в stream-json: в плоском тексте нет токенов и фактической
    модели, поэтому задание закрывалось с пустым effective_model и без расхода.
    """
    cwd = _clean_cwd(cwd)
    provider, model = _hermes_model(requested_model, requested_profile)
    cmd = [HERMES_EXE, "chat", "--query-file", "-", "--oneshot", "--quiet",
           "--source", "tool", "--format", "stream-json"]
    if provider:
        cmd += ["--provider", provider]
    if model:
        cmd += ["--model", model]
    if cwd:
        cmd += ["--in", cwd]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, input=prompt.encode("utf-8"),
            timeout=timeout, cwd=cwd, creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_result(exc, started)
    raw = proc.stdout.decode("utf-8", "replace")
    parsed = _hermes_json_result(raw)
    err = ANSI_RE.sub("", proc.stderr.decode("utf-8", "replace")).strip()
    if parsed:
        final, usage, model_used = parsed
    else:
        # Старый плоский режим — на случай, если JSON-поток не пришёл.
        out = ANSI_RE.sub("", raw)
        lines = [line for line in out.splitlines()
                 if not re.match(r"^\s*session(?:\s+id)?\s*[:=]", line, re.I)]
        final, usage, model_used = "\n".join(lines).strip(), {}, None
    exit_code = proc.returncode
    error = None
    if exit_code == 0 and not final:
        exit_code = 1
        error = err or "Hermes CLI returned no final answer"
    return {
        "output": final or err[-2000:],
        "usage": usage,
        "exit_code": exit_code,
        "model": model_used,
        **({"error": error} if error else {}),
        "elapsed": round(time.time() - started, 2),
    }

def run_kimi(prompt: str, timeout: int, requested_model: str | None = None,
             requested_profile: str | None = None, reasoning_effort: str | None = None,
             *, cwd: str | None = None) -> dict:
    """Kimi Code через локальный API приложения (без окна и CDP)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from kimi_client import KimiClient
    started = time.time()
    try:
        result = KimiClient().run(prompt, model=requested_model, effort=reasoning_effort,
                                  cwd=cwd, timeout=timeout)
        result["elapsed"] = round(time.time() - started, 2)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"output": "", "usage": {}, "exit_code": 1, "elapsed": round(time.time() - started, 2),
                "error": str(exc)}


def run_channel(prompt: str, timeout: int, requested_model: str | None = None,
                requested_profile: str | None = None, reasoning_effort: str | None = None,
                *, cwd: str | None = None) -> dict:
    """Канал внешнего API: запрос выполняет хаб, ключ остаётся на сервере."""
    import urllib.error
    import urllib.request
    started = time.time()
    channel_id = (requested_profile or "").split(":", 1)[-1].strip()
    hub = (os.environ.get("HUB_URL") or "http://127.0.0.1:8090").rstrip("/")
    token = os.environ.get("HUB_TOKEN") or TOKEN
    if not channel_id:
        return {"output": "", "usage": {}, "exit_code": 1,
                "error": "не выбран API-канал (profile channel:<id>)",
                "elapsed": round(time.time() - started, 2)}
    body = json.dumps({"prompt": prompt, "model": requested_model or "",
                       "timeout": max(10, min(int(timeout), 900))},
                      ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(f"{hub}/channels/{channel_id}/chat", data=body,
                                     method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=int(timeout) + 20) as response:
            data = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("detail")
        except Exception:  # noqa: BLE001
            detail = None
        text = detail if isinstance(detail, str) else None
        return {"output": "", "usage": {}, "exit_code": 1,
                "error": text or f"хаб ответил HTTP {exc.code}", "elapsed": round(time.time() - started, 2)}
    except Exception as exc:  # noqa: BLE001
        return {"output": "", "usage": {}, "exit_code": 1, "error": str(exc),
                "elapsed": round(time.time() - started, 2)}
    output = data.get("output") if isinstance(data.get("output"), str) else ""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if not output.strip():
        return {"output": "", "usage": {}, "exit_code": 1,
                "error": "канал вернул пустой ответ", "elapsed": round(time.time() - started, 2)}
    if data.get("reported_model"):
        usage = dict(usage)
        usage["reported_model"] = data["reported_model"]
    return {"output": output.strip(), "usage": usage, "exit_code": 0,
            "elapsed": round(time.time() - started, 2)}


# Агент с локальной копией проектов: тот же Codex, но объявляется отдельным
# id, чтобы задания серверных проектов не уезжали на ноутбук.
AGENT_ALIASES = {"codex-laptop": "codex"}

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
    "kimi": {
        "name": "Kimi Code (API приложения)",
        "runner": run_kimi,
        "path": sys.executable,
        "timeout": 900,
    },
    "channel": {
        "name": "API-канал (внешний провайдер)",
        "runner": run_channel,
        "path": sys.executable,
        "timeout": 900,
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
        if self.path == "/catalog/codex":
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            self._send(200, codex_model_catalog())
            return
        if self.path == "/agents":
            if not self._authorized():
                self._send(401, {"error": "unauthorized"})
                return
            items = []
            for name, spec in AGENTS.items():
                path = _agent_path(name, spec)
                items.append({
                    "id": name,
                    "name": spec["name"],
                    "available": bool(path) and os.path.exists(path),
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
        agent_exe = _agent_path(agent_id, spec)
        if not os.path.exists(agent_exe):
            self._send(503, {"error": f"agent binary not found: {agent_exe}"})
            return

        timeout = int(payload.get("timeout") or spec["timeout"])
        model = (payload.get("model") or "").strip() or None
        profile = (payload.get("profile") or "").strip() or None
        reasoning_effort = (payload.get("reasoning_effort") or "").strip() or None
        cwd = payload.get("cwd")
        lock = LOCKS.setdefault(agent_id, threading.Lock())
        if not lock.acquire(blocking=False):
            self._send(429, {"error": f"agent '{agent_id}' busy"})
            return
        try:
            if (model or profile) and agent_id not in ("codex", "qwen", "openclaw", "hermes", "kimi"):
                self._send(400, {"error": f"agent '{agent_id}' does not support model selection"})
                return
            if reasoning_effort:
                if agent_id == "qwen":
                    qwen_desktop_mode(reasoning_effort)
                elif agent_id == "kimi":
                    if reasoning_effort not in ("low", "high", "max"):
                        self._send(400, {"error": f"unsupported reasoning_effort '{reasoning_effort}'",
                                         "supported": ["low", "high", "max"]})
                        return
                elif agent_id == "openclaw":
                    if reasoning_effort not in OPENCLAW_LEVELS:
                        self._send(400, {"error": f"unsupported reasoning_effort '{reasoning_effort}'",
                                         "supported": list(OPENCLAW_LEVELS)})
                        return
                else:
                    levels = ((codex_model_catalog().get("models") or {}).get(model or "") or {}).get("levels") or []
                    if agent_id != "codex" or not levels:
                        self._send(400, {"error": "reasoning_effort is not supported for this agent/model"})
                        return
                    if reasoning_effort not in levels:
                        self._send(400, {"error": f"unsupported reasoning_effort '{reasoning_effort}'",
                                         "supported": levels})
                        return
            log(f"run {agent_id} prompt_len={len(prompt)} timeout={timeout} "
                f"profile={profile} model={model} effort={reasoning_effort}")
            if agent_id == "codex":
                result = spec["runner"](prompt, timeout, model, profile, cwd=cwd,
                                        reasoning_effort=reasoning_effort)
            elif agent_id == "qwen":
                result = spec["runner"](prompt, timeout, model, profile, cwd=cwd,
                                         reasoning_effort=reasoning_effort)
            elif agent_id in ("openclaw", "kimi"):
                result = spec["runner"](prompt, timeout, model, profile, cwd=cwd,
                                        reasoning_effort=reasoning_effort)
            elif agent_id == "hermes":
                result = spec["runner"](prompt, timeout, model, profile, cwd=cwd)
            else:
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
