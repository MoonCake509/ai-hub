"""DIAG-01 — наблюдения вместо статического флага `status: verified`.

Статус агента — не слово в YAML, а четыре независимых наблюдения:

    configured     карточка реестра заполнена (факт конфигурации, не истекает)
    compatibility  заявленные возможности верны для этой версии адаптера (TTL 7 суток)
    availability   исполнитель отвечает прямо сейчас (TTL 3 минуты, пишут heartbeat/probes)
    quality        последние прогоны приемлемы (TTL 24 часа, пишут завершённые задания)

Состояние вычисляется на чтение: `checked_at + ttl_s < now` → `stale`,
нет наблюдения → `unknown`.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

KINDS = ("configured", "compatibility", "availability", "quality")

KIND_RU = {
    "configured": "конфигурация",
    "compatibility": "совместимость",
    "availability": "доступность",
    "quality": "качество",
}

TTL_COMPATIBILITY_S = int(os.environ.get("COMPATIBILITY_TTL_S", str(7 * 86400)))
TTL_AVAILABILITY_S = int(os.environ.get("AVAILABILITY_TTL_S", "180"))
TTL_QUALITY_S = int(os.environ.get("QUALITY_TTL_S", "86400"))

REQUIRED_FIELDS = ("id", "name", "transport", "host")
TRANSPORTS = ("cli", "http", "cdp", "acp")
SECRET_HINTS = ("token", "secret", "password", "authorization", "cookie", "apikey", "api_key")

DDL = [
    """create table if not exists aihub.agent_observations (
        id text primary key,
        agent_id text not null,
        kind text not null,
        state text not null,
        source text not null,
        checked_at timestamptz not null,
        ttl_s int,
        reason text,
        detail jsonb not null default '{}',
        recorded_at timestamptz not null default now()
    )""",
    """create index if not exists agent_observations_latest
        on aihub.agent_observations(agent_id, kind, checked_at desc)""",
    """create index if not exists agent_observations_checked
        on aihub.agent_observations(checked_at desc)""",
]

_registry_checked_at = None


def now():
    return datetime.now(timezone.utc)


def _parse(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value):
    parsed = _parse(value)
    return parsed.isoformat(timespec="seconds") if parsed else None


def age_seconds(checked_at, at=None):
    parsed = _parse(checked_at)
    if parsed is None:
        return None
    return max(0, int(((at or now()) - parsed).total_seconds()))


def _clean_detail(detail):
    """Оставляет только несекретные скалярные поля: detail идёт в UI и логи."""
    out = {}
    for key, value in (detail or {}).items():
        name = str(key)
        if any(hint in name.lower() for hint in SECRET_HINTS):
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            out[name] = value
        elif isinstance(value, (list, tuple)):
            out[name] = [item for item in value if isinstance(item, (str, int, float, bool))][:50]
        elif isinstance(value, dict):
            out[name] = _clean_detail(value)
    return out


def observation(kind, state, checked_at, ttl_s, source, reason=None, detail=None):
    cleaned_reason = (reason or "").strip() or None
    if state != "ok" and not cleaned_reason:
        cleaned_reason = "причина не указана"
    return {
        "kind": kind,
        "state": state,
        "source": source,
        "checked_at": _iso(checked_at),
        "ttl_s": ttl_s,
        "age_s": age_seconds(checked_at),
        "reason": cleaned_reason,
        "detail": _clean_detail(detail),
    }


def resolve_state(stored_state, checked_at, ttl_s, at=None):
    """Состояние на чтение: TTL истёк → stale, нет данных → unknown."""
    checked = _parse(checked_at)
    if not stored_state or checked is None:
        return "unknown"
    if ttl_s is not None and checked + timedelta(seconds=int(ttl_s)) < (at or now()):
        return "stale"
    return stored_state


def configured_facts(agent):
    problems = []
    for field in REQUIRED_FIELDS:
        if not str(agent.get(field) or "").strip():
            problems.append("не заполнено поле " + field)
    transport = str(agent.get("transport") or "")
    if transport and transport not in TRANSPORTS:
        problems.append("неизвестный транспорт " + transport)
    if agent.get("configured") is False:
        problems.append("карточка помечена configured: false")
    if "configured" not in agent and agent.get("status") not in (None, "verified"):
        problems.append("status " + str(agent.get("status")) + " без подтверждённой конфигурации")
    profiles = [p for p in (agent.get("profiles") or []) if isinstance(p, dict)]
    models = sum(len(p.get("models") or []) for p in profiles)
    detail = {
        "transport": transport or None,
        "host": agent.get("host"),
        "bridge_agent": agent.get("bridge_agent"),
        "profiles": len(profiles),
        "models": models,
    }
    return (not problems), ("; ".join(problems) or None), detail


def compatibility_facts(agent, adapter_version):
    """Заявленные возможности для текущей версии адаптера.

    Пробы здесь не делаются: подтверждение реальной работы — DIAG-02.
    """
    profiles = [p for p in (agent.get("profiles") or []) if isinstance(p, dict)]
    problems = []
    if not profiles:
        problems.append("нет профилей провайдеров")
    if agent.get("model_selection") and not any(p.get("models") for p in profiles):
        problems.append("включён выбор модели, но ни в одном профиле нет моделей")
    state = "degraded" if problems else "ok"
    detail = {
        "adapter_version": adapter_version,
        "supports_workdir": bool(agent.get("supports_workdir")),
        "model_selection": bool(agent.get("model_selection")),
        "input_modalities": list(agent.get("input_modalities") or ["text"]),
        "output_modalities": list(agent.get("output_modalities") or ["text"]),
    }
    return state, ("; ".join(problems) or None), detail


def summarize(observations):
    """Сводка для списка: статус хаба сюда не подставляется."""
    states = {kind: (observations.get(kind) or {}).get("state", "unknown") for kind in KINDS}
    if states["configured"] == "failed":
        return "failed", (observations["configured"] or {}).get("reason")
    for kind in KINDS:
        if states[kind] == "failed":
            return "failed", KIND_RU[kind] + ": " + str((observations[kind] or {}).get("reason"))
    if states["availability"] == "unknown":
        reason = (observations["availability"] or {}).get("reason") or "нет данных"
        return "unknown", "нет данных о доступности: " + reason
    for kind in KINDS:
        if states[kind] in ("degraded", "stale"):
            return "degraded", KIND_RU[kind] + ": " + str((observations[kind] or {}).get("reason"))
    return "ok", None


def _human_age(seconds):
    if seconds < 60:
        return str(seconds) + " с"
    minutes = seconds // 60
    if minutes < 60:
        return str(minutes) + " мин"
    hours = minutes // 60
    if hours < 48:
        return str(hours) + " ч"
    return str(hours // 24) + " суток"


def adapter_version():
    return os.environ.get("HUB_ADAPTER_VERSION") or VERSION_FALLBACK


def _evaluate_one(agent, stored, at, store_error, version):
    agent_id = str(agent.get("id") or "")
    checked = _registry_checked_at or at

    configured_ok, configured_reason, configured_detail = configured_facts(agent)
    observations = {
        "configured": observation("configured", "ok" if configured_ok else "failed", checked,
                                  None, "registry",
                                  None if configured_ok else configured_reason, configured_detail),
    }
    compat_state, compat_reason, compat_detail = compatibility_facts(agent, version)
    observations["compatibility"] = observation("compatibility", compat_state, checked,
                                                TTL_COMPATIBILITY_S, "registry",
                                                compat_reason, compat_detail)

    defaults = {
        "availability": (TTL_AVAILABILITY_S, "нет heartbeat: ни одного наблюдения доступности"),
        "quality": (TTL_QUALITY_S, "нет завершённых прогонов"),
    }
    for kind, (ttl_s, empty_reason) in defaults.items():
        row = stored.get(kind)
        if row is None:
            reason = empty_reason if not store_error else store_error
            observations[kind] = observation(kind, "unknown", None, ttl_s, "нет наблюдений", reason)
            continue
        state = resolve_state(row.get("state"), row.get("checked_at"), row.get("ttl_s"), at)
        reason = row.get("reason")
        if state == "stale":
            age = age_seconds(row.get("checked_at"), at) or 0
            reason = ("последнее наблюдение " + _human_age(age) + " назад, TTL "
                      + str(row.get("ttl_s")) + " с")
        observations[kind] = observation(kind, state, row.get("checked_at"), row.get("ttl_s"),
                                         row.get("source") or "unknown", reason, row.get("detail"))

    effective, effective_reason = summarize(observations)
    status = "verified" if (observations["configured"]["state"] == "ok"
                            and observations["compatibility"]["state"] == "ok") else "unverified"
    return {
        "id": agent_id,
        "status": status,
        "observations": observations,
        "effective_state": effective,
        "effective_reason": effective_reason,
        "checked_at": _iso(checked),
    }


def evaluate(agents, at=None, version=None):
    """Наблюдения для списка карточек. Недоступная БД даёт unknown, а не 500."""
    moment = at or now()
    version = version or adapter_version()
    store_error = None
    try:
        stored = latest()
    except Exception as exc:  # noqa: BLE001
        stored = {}
        store_error = "хранилище наблюдений недоступно: " + str(exc)[:120]
    out = {}
    for agent in agents:
        agent_id = str(agent.get("id") or "")
        out[agent_id] = _evaluate_one(agent, stored.get(agent_id, {}), moment, store_error, version)
    return out


def evaluate_one(agent, at=None, version=None):
    return evaluate([agent], at, version)[str(agent.get("id") or "")]


def init():
    with db._lock:
        conn = db.connect()
        for stmt in DDL:
            conn.run(db._qualify(stmt))


def record(agent_id, kind, state, source, checked_at=None, ttl_s=None, reason=None, detail=None):
    """Сохранить наблюдение. Источник времени — вызывающий: heartbeat, прогон, реестр."""
    item = observation(kind, state, checked_at or now(), ttl_s, source, reason, detail)
    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "insert into aihub.agent_observations"
            "(id, agent_id, kind, state, source, checked_at, ttl_s, reason, detail)"
            " values (:id, :a, :k, :s, :src, :ts, :ttl, :r, :d::jsonb)"),
            id=uuid.uuid4().hex, a=agent_id, k=kind, s=state, src=source,
            ts=_parse(item["checked_at"]), ttl=ttl_s, r=item["reason"],
            d=json.dumps(item["detail"], ensure_ascii=False))
    return item


def latest():
    """Последнее сохранённое наблюдение по каждой паре (agent_id, kind)."""
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "select distinct on (agent_id, kind)"
            " agent_id, kind, state, source, checked_at, ttl_s, reason, detail"
            " from aihub.agent_observations"
            " order by agent_id, kind, checked_at desc, recorded_at desc"))
    out = {}
    for row in rows:
        out.setdefault(str(row["agent_id"]), {})[str(row["kind"])] = row
    return out


def record_registry(agents, at=None, version=None):
    """Реестровые наблюдения при старте: configured и compatibility."""
    global _registry_checked_at
    moment = at or now()
    version = version or adapter_version()
    _registry_checked_at = moment
    written = 0
    for agent in agents:
        agent_id = str(agent.get("id") or "")
        if not agent_id:
            continue
        configured_ok, reason, detail = configured_facts(agent)
        record(agent_id, "configured", "ok" if configured_ok else "failed", "registry",
               moment, None, None if configured_ok else reason, detail)
        state, compat_reason, compat_detail = compatibility_facts(agent, version)
        record(agent_id, "compatibility", state, "registry", moment, TTL_COMPATIBILITY_S,
               compat_reason, compat_detail)
        written += 2
    try:
        prune()
    except Exception as exc:  # noqa: BLE001 — уборка не должна мешать старту
        print("observations prune failed: " + str(exc), flush=True)
    return written


def prune(keep_days=30):
    cutoff = now() - timedelta(days=keep_days)
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "delete from aihub.agent_observations where checked_at < :c returning id"), c=cutoff)
    return len(rows)


def public(item):
    """Только поля контракта DIAG-01, без внутренних ключей."""
    return {
        "id": item.get("id"),
        "status": item.get("status"),
        "effective_state": item.get("effective_state"),
        "effective_reason": item.get("effective_reason"),
        "checked_at": item.get("checked_at"),
        "observations": item.get("observations") or {},
    }


VERSION_FALLBACK = "0.3.0"
