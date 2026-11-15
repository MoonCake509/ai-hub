"""Каналы внешних провайдеров (OpenAI-совместимый API).

Владелец подключает провайдера формой: название, адрес API, ключ, ID модели.
Ключ живёт только на сервере: он не входит в ответы API и не передаётся
воркеру — воркер просит хаб выполнить запрос (`POST /channels/{id}/chat`),
а сам хаб ходит к провайдеру. Браузер видит только название, адрес, модели и
маску ключа.

Ошибка провайдера возвращается как есть. Молчаливого перехода на другую
модель нет: если модель ответила ошибкой, это ошибка именно этой модели.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from urllib.parse import urlparse

import httpx

from . import db

SECRET = (os.environ.get("CHANNELS_SECRET") or os.environ.get("HUB_TOKEN") or "aihub-local").encode()
PROBE_PROMPT = "Ответь одним словом: ок"
PROBE_MAX_TOKENS = 256
DEFAULT_MAX_TOKENS = 2048
MAX_PROBE_SECONDS = 120


def _key() -> bytes:
    return hashlib.sha256(b"aihub-channels:" + SECRET).digest()


def _keystream(length: int) -> bytes:
    out = bytearray()
    counter = 0
    key = _key()
    while len(out) < length:
        out.extend(hashlib.sha256(key + counter.to_bytes(4, "big")).digest())
        counter += 1
    return bytes(out[:length])


def encrypt(plain: str) -> str:
    raw = (plain or "").encode("utf-8")
    stream = _keystream(len(raw))
    body = bytes(a ^ b for a, b in zip(raw, stream))
    tag = hmac.new(_key(), body, hashlib.sha256).digest()[:16]
    return "enc1:" + base64.b64encode(body + tag).decode("ascii")


def decrypt(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if not text.startswith("enc1:"):
        # Старые/ручные строки: считаем, что ключ лежит как есть.
        return text
    try:
        blob = base64.b64decode(text[5:])
    except Exception:  # noqa: BLE001
        return ""
    body, tag = blob[:-16], blob[-16:]
    if not hmac.compare_digest(tag, hmac.new(_key(), body, hashlib.sha256).digest()[:16]):
        return ""
    stream = _keystream(len(body))
    return bytes(a ^ b for a, b in zip(body, stream)).decode("utf-8", "replace")


def mask(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return key[:4] + "…" + key[-4:]


def normalize_base_url(url: str) -> str:
    value = (url or "").strip().rstrip("/")
    if not value:
        raise ValueError("адрес API не задан")
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("адрес API должен начинаться с http:// или https://")
    return value


def endpoint(base_url: str) -> str:
    base = normalize_base_url(base_url)
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def parse_models(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    out: list[str] = []
    for item in values or []:
        for part in re.split(r"[\n,]+", str(item)):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def list_channels() -> list[dict]:
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, "select * from aihub.channels order by created_at")
    return [public(row) for row in rows]


def _row(channel_id: str) -> dict | None:
    with db._lock:
        conn = db.connect()
        return db._one(conn, "select * from aihub.channels where id = :i", i=channel_id)


def public(row: dict) -> dict:
    models = row.get("models")
    if isinstance(models, str):
        try:
            models = json.loads(models)
        except ValueError:
            models = []
    last_test = row.get("last_test")
    if isinstance(last_test, str):
        try:
            last_test = json.loads(last_test)
        except ValueError:
            last_test = None
    key = decrypt(row.get("api_key") or "")
    return {
        "id": row["id"],
        "agent_id": "channel:" + row["id"],
        "name": row.get("name") or "",
        "base_url": row.get("base_url") or "",
        "models": models or [],
        "key_masked": mask(key),
        "has_key": bool(key),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "last_test": last_test,
    }


def _iso(value) -> str | None:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def create(name: str, base_url: str, api_key: str, models) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("название не задано")
    if len(name) > 80:
        raise ValueError("название длиннее 80 символов")
    base_url = normalize_base_url(base_url)
    if not (api_key or "").strip():
        raise ValueError("ключ не задан")
    model_list = parse_models(models)
    if not model_list:
        raise ValueError("ID модели не задан")
    if len(model_list) > 50:
        raise ValueError("слишком много моделей: максимум 50")
    channel_id = uuid.uuid4().hex
    with db._lock:
        conn = db.connect()
        exists = db._one(conn, "select id from aihub.channels where lower(name) = lower(:n)", n=name)
        if exists:
            raise ValueError(f"канал '{name}' уже подключён")
        conn.run(
            "insert into aihub.channels(id, name, base_url, api_key, models)"
            " values (:i, :n, :u, :k, :m::jsonb)",
            i=channel_id, n=name, u=base_url, k=encrypt(api_key.strip()),
            m=json.dumps(model_list, ensure_ascii=False))
    return public(_row(channel_id))


def update(channel_id: str, *, name=None, base_url=None, api_key=None, models=None) -> dict:
    row = _row(channel_id)
    if not row:
        raise KeyError(channel_id)
    fields = []
    params: dict = {"i": channel_id}
    if name is not None:
        clean = str(name).strip()
        if not clean:
            raise ValueError("название не задано")
        fields.append("name = :n")
        params["n"] = clean
    if base_url is not None:
        fields.append("base_url = :u")
        params["u"] = normalize_base_url(base_url)
    if api_key is not None and str(api_key).strip():
        fields.append("api_key = :k")
        params["k"] = encrypt(str(api_key).strip())
    if models is not None:
        model_list = parse_models(models)
        if not model_list:
            raise ValueError("ID модели не задан")
        fields.append("models = :m::jsonb")
        params["m"] = json.dumps(model_list, ensure_ascii=False)
    if not fields:
        return public(row)
    fields.append("updated_at = now()")
    with db._lock:
        conn = db.connect()
        conn.run("update aihub.channels set " + ", ".join(fields) + " where id = :i", **params)
    return public(_row(channel_id))


def delete(channel_id: str) -> bool:
    with db._lock:
        conn = db.connect()
        found = db._one(conn, "select id from aihub.channels where id = :i", i=channel_id)
        if not found:
            return False
        conn.run("delete from aihub.channels where id = :i", i=channel_id)
    return True


def _save_test(channel_id: str, result: dict) -> None:
    with db._lock:
        conn = db.connect()
        conn.run("update aihub.channels set last_test = :t::jsonb where id = :i",
                 i=channel_id, t=json.dumps(result, ensure_ascii=False))


def _error_text(response: httpx.Response) -> str:
    text = (response.text or "").strip()
    try:
        data = response.json()
    except ValueError:
        return text[:800] or f"HTTP {response.status_code}"
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("code") or json.dumps(err, ensure_ascii=False)
            return f"HTTP {response.status_code}: {detail}"[:800]
        if isinstance(err, str):
            return f"HTTP {response.status_code}: {err}"[:800]
        if data.get("message"):
            return f"HTTP {response.status_code}: {data['message']}"[:800]
    return f"HTTP {response.status_code}: {text[:600]}"


def _content_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    content = ((choices[0] or {}).get("message") or {}).get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") if isinstance(part, dict) else str(part)
                          for part in content)
    return content.strip() if isinstance(content, str) else ""


def _usage(data: dict) -> dict | None:
    raw = data.get("usage")
    if not isinstance(raw, dict):
        return None
    prompt = raw.get("prompt_tokens", raw.get("input_tokens"))
    completion = raw.get("completion_tokens", raw.get("output_tokens"))
    total = raw.get("total_tokens")
    if prompt is None and completion is None and total is None:
        return None
    try:
        prompt = int(prompt or 0)
        completion = int(completion or 0)
        total = int(total if total is not None else prompt + completion)
    except (TypeError, ValueError):
        return None
    return {"input_tokens": prompt, "output_tokens": completion, "total_tokens": total}


def call(channel: dict, model: str, prompt: str, timeout: int = 120,
         max_tokens: int = DEFAULT_MAX_TOKENS) -> dict:
    """Один реальный вызов. Возвращает текст, usage, задержку или исключение."""
    key = decrypt(channel.get("api_key") or "")
    if not key:
        raise RuntimeError("ключ канала не найден на сервере — введите его заново")
    route = (
        f"Ты отвечаешь внутри AI Hub через API-канал «{channel.get('name') or 'канал'}». "
        f"Фактическая модель этого запроса: {model}. "
        "Если спрашивают, на какой модели ты работаешь, называй именно её "
        "и не приписывай себе другую модель или компанию."
    )
    body = {
        "model": model,
        "messages": [{"role": "system", "content": route},
                     {"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "stream": False,
    }
    started = time.time()
    try:
        response = httpx.post(endpoint(channel.get("base_url") or ""), json=body,
                              headers={"Authorization": f"Bearer {key}",
                                       "Content-Type": "application/json"},
                              timeout=httpx.Timeout(connect=15.0, read=timeout,
                                                    write=30.0, pool=15.0))
    except httpx.TimeoutException:
        waited = int(time.time() - started)
        raise RuntimeError(f"провайдер не ответил за {waited} с (лимит ожидания {timeout} с)") from None
    except httpx.HTTPError as exc:
        raise RuntimeError(f"сетевая ошибка: {exc}") from None
    elapsed_ms = int((time.time() - started) * 1000)
    if response.status_code >= 400:
        raise RuntimeError(_error_text(response))
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("провайдер вернул не JSON") from None
    text = _content_text(data)
    if not text:
        choices = data.get("choices") or []
        message = ((choices[0] or {}).get("message") or {}) if choices else {}
        if (message.get("reasoning_content") or "").strip():
            raise RuntimeError("модель израсходовала лимит на размышление и не дала ответ "
                               f"(max_tokens={max_tokens}); увеличьте лимит или проверьте другую модель")
        raise RuntimeError("провайдер вернул пустой ответ")
    return {
        "output": text,
        "usage": _usage(data),
        "elapsed_ms": elapsed_ms,
        "reported_model": data.get("model") if isinstance(data.get("model"), str) else None,
    }


def probe(channel_id: str, model: str | None = None) -> dict:
    row = _row(channel_id)
    if not row:
        raise KeyError(channel_id)
    models = row.get("models") or []
    if isinstance(models, str):
        models = json.loads(models)
    chosen = (model or "").strip() or (models[0] if models else "")
    if not chosen:
        raise ValueError("у канала нет модели для проверки")
    started = time.time()
    result = {"model": chosen, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    try:
        answer = call(row, chosen, PROBE_PROMPT, timeout=MAX_PROBE_SECONDS,
                      max_tokens=PROBE_MAX_TOKENS)
        result.update({
            "ok": True,
            "answer": answer["output"],
            "elapsed_ms": answer["elapsed_ms"],
            "usage": answer["usage"],
            "reported_model": answer.get("reported_model"),
            "error": None,
        })
    except Exception as exc:  # noqa: BLE001
        result.update({
            "ok": False,
            "answer": None,
            "elapsed_ms": int((time.time() - started) * 1000),
            "usage": None,
            "reported_model": None,
            "error": str(exc)[:800],
        })
    _save_test(channel_id, result)
    return result


def agent_dict(row: dict) -> dict:
    view = public(row)
    cid = view["id"]
    profile = f"channel:{cid}"
    models = view["models"] or []
    return {
        "id": profile,
        "name": view["name"],
        "host": "server",
        "transport": "http",
        "bridge_agent": "channel",
        "model_selection": True,
        "default_profile": profile,
        "profiles": [{
            "id": profile,
            "name": view["name"],
            "models": models,
        }],
        "capabilities": {
            "text": {"supported": True, "reason": "", "evidence": "probe"},
            "patch": {"supported": False, "reason": "API-канал не правит файлы; это делают исполнители AI Hub", "evidence": "catalog"},
            "workdir": {"supported": False, "reason": "API-канал не работает в папке проекта", "evidence": "catalog"},
            "commands": {"supported": False, "reason": "API-канал не запускает команды", "evidence": "catalog"},
            "usage": {"supported": True, "reason": "", "evidence": "probe"},
            "streaming": {"supported": False, "reason": "ответ получаем целиком", "evidence": "catalog"},
            "stop": {"supported": False, "reason": "остановка неподтверждена", "evidence": "catalog"},
            "output_control": {"supported": False, "reason": "ограничение вывода не подтверждено", "evidence": "catalog"},
            "thinking": {"supported": False, "reason": "уровни reasoning не заданы для канала", "evidence": "catalog"},
        },
        "supports_workdir": False,
        "filesystem_access": False,
        "context_mode": "Текст задачи и история чата; файлы, Git и тесты — исполнители AI Hub",
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "tool": False,
        "kind": "channel",
        "channel_id": cid,
        "enabled": True,
    }


def agent_dicts() -> list[dict]:
    try:
        with db._lock:
            conn = db.connect()
            rows = db._rows(conn, "select * from aihub.channels order by created_at")
    except Exception:  # noqa: BLE001 — реестр не должен падать из-за каналов
        return []
    return [agent_dict(row) for row in rows]
