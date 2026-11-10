"""DIAG-02 — исполнитель лёгких проб.

Проба отвечает на один узкий вопрос и обязана назвать причину отказа:
закрытый порт, таймаут, 401, 5xx, «200, но не JSON», «JSON без нужной
модели», «бинарь есть, но в другом месте». Пассивные пробы не делают
платных вызовов: видов проб, отправляющих prompt модели, здесь нет.

Результат каждой пробы записывается в `agent_observations` как
наблюдение `availability` с TTL 180 с (источник `probe:<kind>`).
Дедупликация — по виду и нормализованной цели: два агента на одном
адресе дают один сетевой вызов.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import observations

DEFAULT_TIMEOUT_S = int(os.environ.get("PROBE_TIMEOUT_S", "5"))
TOTAL_DEADLINE_S = int(os.environ.get("PROBE_DEADLINE_S", "20"))
MAX_WORKERS = int(os.environ.get("PROBE_WORKERS", "4"))
AVAILABILITY_TTL_S = int(os.environ.get("AVAILABILITY_TTL_S", "180"))
MAX_BODY = 200_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db_host() -> str:
    parsed = urllib.parse.urlsplit(os.environ.get("DATABASE_URL", ""))
    return parsed.hostname or "postgres"


def _expand(value):
    """Подставляет окружение в поля пробы: `{db_host}` и `{gateway}`."""
    if not isinstance(value, str):
        return value
    value = value.replace("{db_host}", _db_host())
    return value.replace("{gateway}", os.environ.get("AIHUB_HOST_GATEWAY", "172.18.0.1"))


def _target_of(probe: dict) -> str:
    kind = probe.get("kind")
    if kind == "http_json":
        url = str(_expand(probe.get("url") or ""))
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")) if url else ""
    if kind == "cli_binary":
        return str(probe.get("binary") or "")
    if kind == "tcp_port":
        return str(_expand(probe.get("host") or "")) + ":" + str(probe.get("port") or "")
    return ""


def plan(agents: list[dict], services: list[dict] | None = None) -> list[dict]:
    """Что и по каким ключам будет проверяться. Сетевых вызовов нет.

    `services` — пассивные пробы сервисов, которые видит сам хаб (docker-сеть);
    пробы агентов на хосте выполняет воркер (DIAG-03).
    """
    items = []
    for agent in list(agents) + list(services or []):
        probe = agent.get("probe")
        if not isinstance(probe, dict) or not probe.get("kind"):
            continue
        target = _target_of(probe)
        if not target:
            continue
        items.append({
            "agent_id": str(agent.get("id") or ""),
            "kind": str(probe.get("kind")),
            "target": target,
            "dedup_key": str(probe.get("kind")) + "|" + target,
            "timeout_s": int(probe.get("timeout_s") or DEFAULT_TIMEOUT_S),
            "probe": probe,
        })
    return items


def _resolve(document, path: str) -> list:
    """Значения по пути с поддержкой списков: `data[].id`, `meta.version`."""
    current = [document]
    for part in path.split("."):
        nxt = []
        as_list = part.endswith("[]")
        key = part[:-2] if as_list else part
        for node in current:
            if key:
                if not isinstance(node, dict) or key not in node:
                    continue
                node = node[key]
            if as_list:
                if isinstance(node, list):
                    nxt.extend(node)
            else:
                nxt.append(node)
        current = nxt
    return current


def _json_problem(document, expect: dict) -> str | None:
    for path in expect.get("require_keys") or []:
        if not _resolve(document, path):
            return "в ответе нет поля " + path
    for path, wanted in (expect.get("require_equals") or {}).items():
        values = _resolve(document, path)
        if not values:
            return "в ответе нет поля " + path
        if wanted not in values:
            shown = ", ".join(str(v) for v in values[:5])
            return "в " + path + " нет значения " + str(wanted) + " (есть: " + shown + ")"
    return None


def probe_http_json(spec: dict, timeout_s: float) -> dict:
    url = str(spec.get("url") or "")
    expect = spec.get("expect") or {}
    method = str(spec.get("method") or "GET").upper()
    headers = {}
    for name, env_name in (spec.get("headers_env") or {}).items():
        value = os.environ.get(str(env_name))
        if value:
            headers[str(name)] = value
    bearer = spec.get("bearer_env")
    if bearer:
        secret = os.environ.get(str(bearer))
        if secret:
            headers["Authorization"] = "Bearer " + secret
    started = time.monotonic()

    def done(state, reason, http_status=None, content_type=None, extra=None):
        detail = {"http_status": http_status, "content_type": content_type,
                  "latency_ms": int((time.monotonic() - started) * 1000)}
        detail.update(extra or {})
        return {"state": state, "reason": reason, "detail": detail}

    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            status = response.status
            content_type = response.headers.get("content-type", "")
            body = response.read(MAX_BODY)
    except urllib.error.HTTPError as exc:
        status = exc.code
        content_type = exc.headers.get("content-type", "") if exc.headers else ""
        if status in (401, 403):
            return done("failed", "неверная авторизация: HTTP " + str(status), status, content_type)
        if status >= 500:
            return done("failed", "сервис ответил HTTP " + str(status), status, content_type)
        return done("failed", "эндпоинт ответил HTTP " + str(status), status, content_type)
    except (socket.timeout, TimeoutError):
        return done("failed", "таймаут " + str(timeout_s) + " с")
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return done("failed", "таймаут " + str(timeout_s) + " с")
        if isinstance(reason, ConnectionRefusedError):
            return done("failed", "порт закрыт: соединение отклонено")
        return done("failed", "нет соединения: " + str(reason)[:120])
    except OSError as exc:
        return done("failed", "сетевая ошибка: " + str(exc)[:120])

    wanted_status = expect.get("status", 200)
    if status != wanted_status:
        return done("failed", "ожидался HTTP " + str(wanted_status) + ", получен " + str(status),
                    status, content_type)
    if expect.get("json", True):
        try:
            document = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return done("failed", "HTTP " + str(status) + ", но в ответе не JSON (" + (content_type or "без content-type") + ")",
                        status, content_type)
        problem = _json_problem(document, expect)
        if problem:
            return done("failed", "канал отвечает, но " + problem, status, content_type)
    return done("ok", None, status, content_type)


def _find_binary(spec: dict) -> tuple[str | None, list[str]]:
    """Ищет файл: сначала зарегистрированный путь, затем PATH и типовые каталоги."""
    name = str(spec.get("binary") or "")
    registered = str(spec.get("path") or "")
    tried = []
    if registered:
        tried.append(registered)
        if os.path.exists(registered):
            return registered, tried
    found = shutil.which(name)
    if found:
        tried.append(found)
        return found, tried
    for pattern in spec.get("search") or []:
        for candidate in sorted(glob.glob(str(pattern)), reverse=True):
            tried.append(candidate)
            if os.path.exists(candidate):
                return candidate, tried
    return None, tried


def probe_cli_binary(spec: dict, timeout_s: float) -> dict:
    if not spec.get("binary"):
        return {"state": "failed", "reason": "в пробе не задан binary", "detail": {}}
    registered = str(spec.get("path") or "")
    found, tried = _find_binary(spec)
    detail = {"registered_path": registered or None, "found_path": found, "searched": len(tried)}
    if not found:
        return {"state": "failed", "reason": "исполняемый файл " + str(spec.get("binary")) + " не найден ни по зарегистрированному пути, ни в PATH", "detail": detail}
    if registered and os.path.normcase(os.path.abspath(registered)) != os.path.normcase(os.path.abspath(found)):
        return {"state": "degraded", "reason": "CLI переместился: зарегистрирован " + registered + ", найден " + found, "detail": detail}
    return {"state": "ok", "reason": None, "detail": detail}


def probe_tcp_port(spec: dict, timeout_s: float) -> dict:
    host = str(_expand(spec.get("host") or ""))
    port = int(spec.get("port") or 0)
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return {"state": "ok", "reason": None,
                    "detail": {"host": host, "port": port, "latency_ms": int((time.monotonic() - started) * 1000)}}
    except (socket.timeout, TimeoutError):
        return {"state": "failed", "reason": "таймаут TCP " + str(timeout_s) + " с", "detail": {"host": host, "port": port}}
    except ConnectionRefusedError:
        return {"state": "failed", "reason": "порт " + str(port) + " закрыт", "detail": {"host": host, "port": port}}
    except OSError as exc:
        return {"state": "failed", "reason": "нет соединения: " + str(exc)[:120], "detail": {"host": host, "port": port}}


RUNNERS = {
    "http_json": probe_http_json,
    "cli_binary": probe_cli_binary,
    "tcp_port": probe_tcp_port,
}


def execute(item: dict, timeout_s: float) -> dict:
    runner = RUNNERS.get(item["kind"])
    started = time.monotonic()
    if runner is None:
        outcome = {"state": "failed", "reason": "неизвестный вид пробы " + item["kind"], "detail": {}}
    else:
        try:
            outcome = runner(item["probe"], timeout_s)
        except Exception as exc:  # noqa: BLE001 — проба не должна ронять прогон
            outcome = {"state": "failed", "reason": "проба упала: " + str(exc)[:160], "detail": {}}
    outcome.setdefault("detail", {})
    outcome["detail"].setdefault("latency_ms", int((time.monotonic() - started) * 1000))
    return {"agent_id": item["agent_id"], "probe": item["kind"], "target": item["target"],
            "dedup_key": item["dedup_key"], **outcome}


def _reuse(item: dict, row: dict, state: str) -> dict:
    return {"agent_id": item["agent_id"], "probe": item["kind"], "target": item["target"],
            "dedup_key": item["dedup_key"], "state": state, "reason": row.get("reason"),
            "detail": dict(row.get("detail") or {}), "reused": True, "deduplicated": False,
            "checked_at": observations._iso(row.get("checked_at"))}


def run(agents: list[dict], agent_id: str | None = None, force: bool = False,
        now: datetime | None = None, services: list[dict] | None = None) -> dict:
    """Прогон проб: дедупликация, общий дедлайн, ограниченная параллельность."""
    moment = now or _now()
    items = plan(agents, services)
    if agent_id:
        items = [item for item in items if item["agent_id"] == agent_id]
    if not items:
        return {"ran": 0, "deduplicated": 0, "reused": 0, "results": []}

    stored = {}
    if not force:
        try:
            stored = observations.latest()
        except Exception:  # noqa: BLE001 — без БД пробы всё равно полезны
            stored = {}

    results = []
    pending = []
    for item in items:
        row = (stored.get(item["agent_id"]) or {}).get("availability")
        state = observations.resolve_state(row.get("state"), row.get("checked_at"), row.get("ttl_s"), moment) if row else "unknown"
        if row and state in ("ok", "degraded", "failed"):
            results.append(_reuse(item, row, state))
        else:
            pending.append(item)

    unique: dict[str, dict] = {}
    for item in pending:
        unique.setdefault(item["dedup_key"], item)
    deadline = time.monotonic() + TOTAL_DEADLINE_S

    def task(key: str) -> tuple[str, dict]:
        item = unique[key]
        remaining = min(float(item["timeout_s"]), deadline - time.monotonic())
        if remaining <= 0.5:
            return key, {"agent_id": item["agent_id"], "probe": item["kind"], "target": item["target"],
                         "dedup_key": key, "state": "failed", "reused": False, "deduplicated": False,
                         "reason": "общий дедлайн прогона " + str(TOTAL_DEADLINE_S) + " с истёк до старта пробы",
                         "detail": {}}
        return key, execute(item, remaining)

    completed: dict[str, dict] = {}
    if unique:
        with ThreadPoolExecutor(max_workers=max(1, min(MAX_WORKERS, len(unique)))) as pool:
            for key, outcome in pool.map(task, list(unique)):
                completed[key] = outcome

    seen: dict[str, int] = {}
    for item in pending:
        outcome = dict(completed.get(item["dedup_key"]) or {})
        outcome["agent_id"] = item["agent_id"]
        outcome.setdefault("target", item["target"])
        outcome.setdefault("probe", item["kind"])
        outcome["dedup_key"] = item["dedup_key"]
        outcome["reused"] = False
        seen[item["dedup_key"]] = seen.get(item["dedup_key"], 0) + 1
        outcome["deduplicated"] = seen[item["dedup_key"]] > 1
        try:
            observations.record(item["agent_id"], "availability", outcome.get("state", "unknown"),
                                "probe:" + item["kind"], moment, AVAILABILITY_TTL_S,
                                outcome.get("reason"), outcome.get("detail"))
        except Exception as exc:  # noqa: BLE001 — запись наблюдения не должна терять результат
            outcome["store_error"] = str(exc)[:120]
        results.append(outcome)

    summary = {
        "ran": len(unique),
        "deduplicated": max(0, len(pending) - len(unique)),
        "reused": sum(1 for item in results if item.get("reused")),
        "results": results,
        "checked_at": observations._iso(moment),
    }
    global _LAST
    _LAST = summary
    return summary


_LAST: dict = {}


def last() -> dict:
    """Последний прогон в памяти процесса (для отладки; правда — agent_observations)."""
    return _LAST
