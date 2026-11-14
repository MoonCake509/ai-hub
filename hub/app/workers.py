"""DIAG-03 — heartbeat воркеров: кто именно исполняет и с какими возможностями.

Воркер присылает не только «я жив», а идентичность исполнителя: хост, ОС-пользователь,
PID, версию протокола и адаптера, путь и версию CLI, набор обслуживаемых агентов.
Хаб пишет по каждому агенту наблюдение `availability` с источником `worker:<id>` и
TTL 180 с; остановленное устройство через три минуты само становится `stale`.

Секреты не собираются: принимаются только поля из белого списка, вложенные
значения прогоняются через тот же фильтр, что и наблюдения DIAG-01.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from . import db, observations
from .protocol import SUPPORTED
from .protocol import PROTOCOL
from .protocol import heartbeat_interval as recommended_heartbeat
from .protocol import lease_risk

SUPPORTED_PROTOCOLS = SUPPORTED
TTL_S = int(os.environ.get("WORKER_TTL_S", "180"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "300"))

DDL = [
    """create table if not exists aihub.workers (
        worker_id text primary key,
        host text,
        os_user text,
        pid int,
        protocol int not null default 2,
        adapter_version text,
        agents jsonb not null default '[]',
        capabilities jsonb not null default '{}',
        identity jsonb not null default '{}',
        started_at timestamptz,
        hb_at timestamptz not null default now()
    )""",
    "create index if not exists workers_hb on aihub.workers(hb_at desc)",
    "alter table aihub.workers add column if not exists hb_interval_s int",
    # Каталог уровней рассуждения, снятый с CLI на самом воркере. Хранится
    # отдельно от heartbeat: он большой и меняется только с версией CLI.
    """create table if not exists aihub.agent_catalog (
        agent_id text primary key,
        worker_id text not null,
        cli_version text,
        catalog jsonb not null default '{}',
        updated_at timestamptz not null default now()
    )""",
]


class ProtocolError(ValueError):
    """Воркер говорит на несовместимой версии протокола."""


def init() -> None:
    with db._lock:
        conn = db.connect()
        for stmt in DDL:
            conn.run(db._qualify(stmt))


def state_of(hb_at, at=None) -> str:
    moment = at or observations.now()
    parsed = observations._parse(hb_at)
    if parsed is None:
        return "unknown"
    if parsed + timedelta(seconds=TTL_S) < moment:
        return "stale"
    return "ok"


def aliases(agents: list[dict] | None) -> dict:
    """Имена, которыми воркер называет агентов → id карточки реестра.

    В реестре карточка зовётся `openclaw-laptop`, а воркер ноутбука — `openclaw`;
    связь задана полем `bridge_agent`, чтобы не дублировать таблицу имён в коде.
    """
    mapping = {}
    for card in agents or []:
        card_id = str(card.get("id") or "").strip()
        if not card_id:
            continue
        mapping[card_id] = card_id
        bridge = str(card.get("bridge_agent") or "").strip()
        if bridge:
            mapping.setdefault(bridge, card_id)
    return mapping


def heartbeat(payload: dict, at=None, agents: list[dict] | None = None) -> dict:
    """Принять heartbeat воркера и записать наблюдения по его агентам."""
    if not isinstance(payload, dict):
        raise ProtocolError("heartbeat must be a JSON object")
    worker_id = str(payload.get("worker_id") or "").strip()
    if not worker_id or len(worker_id) > 120:
        raise ProtocolError("worker_id is required (1..120 chars)")
    try:
        protocol = int(payload.get("protocol") or PROTOCOL)
    except (TypeError, ValueError):
        raise ProtocolError("protocol must be an integer") from None
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ProtocolError("unsupported protocol " + str(protocol))

    moment = at or observations.now()
    agents_raw = payload.get("agents") or []
    if not isinstance(agents_raw, list):
        raise ProtocolError("agents must be a list")
    served = []
    for item in agents_raw[:50]:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            continue
        served.append({
            "id": str(item.get("id")).strip(),
            "cli_path": str(item.get("cli_path") or "") or None,
            "cli_version": str(item.get("cli_version") or "") or None,
            "cli_size": item.get("cli_size") if isinstance(item.get("cli_size"), int) else None,
            "cli_mtime": str(item.get("cli_mtime") or "") or None,
            "capabilities": observations._clean_detail(item.get("capabilities") or {}),
            "ready": item.get("ready") if isinstance(item.get("ready"), bool) else None,
            "problem": str(item.get("problem") or "")[:200] or None,
            # Каталог снимается с CLI воркера; хранится отдельной таблицей, а не
            # в строке workers.agents.
            "catalog": item.get("catalog") if isinstance(item.get("catalog"), dict) else None,
        })
    identity = observations._clean_detail(payload.get("identity") or {})
    capabilities = observations._clean_detail(payload.get("capabilities") or {})
    host = str(payload.get("host") or "")[:200] or None
    os_user = str(payload.get("os_user") or "")[:200] or None
    pid = payload.get("pid") if isinstance(payload.get("pid"), int) else None
    adapter = str(payload.get("adapter_version") or "")[:120] or None
    # CORE-01: воркер сообщает, как часто подтверждает аренду. Если интервал не
    # короче самой аренды, живая попытка станет unknown между подтверждениями —
    # это неверная настройка, и она должна быть видна в /workers, а не выглядеть
    # как потеря связи.
    try:
        hb_interval = int(payload.get("hb_interval_s") or 0) or None
    except (TypeError, ValueError):
        raise ProtocolError("hb_interval_s must be an integer") from None
    risk = lease_risk(hb_interval, LEASE_SECONDS)

    catalogs = {}
    for item in served:
        catalog = item.pop("catalog", None)
        if isinstance(catalog, dict) and (catalog.get("models") or {}):
            catalogs[item["id"]] = catalog

    with db._lock:
        conn = db.connect()
        conn.run(db._qualify(
            "insert into aihub.workers(worker_id, host, os_user, pid, protocol, adapter_version,"
            " hb_interval_s, agents, capabilities, identity, started_at, hb_at)"
            " values (:id, :host, :u, :pid, :p, :av, :hbi, :ag::jsonb, :cap::jsonb, :ident::jsonb, :start, :hb)"
            " on conflict (worker_id) do update set host = excluded.host, os_user = excluded.os_user,"
            " pid = excluded.pid, protocol = excluded.protocol, adapter_version = excluded.adapter_version,"
            " hb_interval_s = excluded.hb_interval_s,"
            " agents = excluded.agents, capabilities = excluded.capabilities, identity = excluded.identity,"
            " started_at = coalesce(aihub.workers.started_at, excluded.started_at), hb_at = excluded.hb_at"),
            id=worker_id, host=host, u=os_user, pid=pid, p=protocol, av=adapter, hbi=hb_interval,
            ag=json.dumps(served, ensure_ascii=False), cap=json.dumps(capabilities, ensure_ascii=False),
            ident=json.dumps(identity, ensure_ascii=False),
            start=observations._parse(payload.get("started_at")), hb=observations._parse(moment))
        # Пустой каталог не затирает рабочий: сбой `codex debug models` не должен
        # лишать интерфейс уже подтверждённых уровней.
        for agent_key, catalog in catalogs.items():
            conn.run(db._qualify(
                "insert into aihub.agent_catalog(agent_id, worker_id, cli_version, catalog, updated_at)"
                " values (:a, :w, :v, :c::jsonb, :at)"
                " on conflict (agent_id) do update set worker_id = excluded.worker_id,"
                " cli_version = excluded.cli_version, catalog = excluded.catalog,"
                " updated_at = excluded.updated_at"),
                a=agent_key, w=worker_id,
                v=next((x.get("cli_version") for x in served if x["id"] == agent_key), None),
                c=json.dumps(catalog, ensure_ascii=False), at=observations._parse(moment))

    written = 0
    names = aliases(agents)
    for item in served:
        canonical = names.get(item["id"], item["id"])
        item["canonical"] = canonical
        detail = {
            "worker_id": worker_id,
            "host": host,
            "os_user": os_user,
            "pid": pid,
            "protocol": protocol,
            "adapter_version": adapter,
            "cli_path": item["cli_path"],
            "cli_version": item["cli_version"],
            "cli_mtime": item["cli_mtime"],
        }
        state, reason = "ok", None
        if item.get("ready") is False:
            state = "degraded"
            reason = str(item.get("problem") or "исполнитель отвечает, но не готов")
        observations.record(canonical, "availability", state, "worker:" + worker_id, moment, TTL_S, reason, detail)
        written += 1
    return {"ok": True, "worker_id": worker_id, "protocol": protocol, "agents": written,
            "ttl_s": TTL_S, "hb_interval_s": hb_interval,
            "heartbeat_interval_s": recommended_heartbeat(LEASE_SECONDS),
            "lease_seconds": LEASE_SECONDS, "lease_risk": risk}


def reasoning_catalog(agent_id: str, at=None) -> dict | None:
    """Уровни рассуждения, подтверждённые CLI на воркере, и их свежесть."""
    with db._lock:
        conn = db.connect()
        row = db._one(conn, db._qualify(
            "select c.agent_id, c.worker_id, c.cli_version, c.catalog, c.updated_at,"
            " w.hb_at from aihub.agent_catalog c"
            " left join aihub.workers w on w.worker_id = c.worker_id"
            " where c.agent_id = :a"), a=agent_id)
    if not row:
        return None
    catalog = row.get("catalog")
    if isinstance(catalog, str):
        try:
            catalog = json.loads(catalog)
        except ValueError:
            catalog = {}
    models = (catalog or {}).get("models") or {}
    if not models:
        return None
    return {
        "models": models,
        "worker_id": row.get("worker_id"),
        "cli_version": row.get("cli_version"),
        "updated_at": observations._iso(row.get("updated_at")),
        "state": state_of(row.get("hb_at"), at),
    }


def reasoning_levels(agent_id: str, model: str | None) -> list[str]:
    """Допустимые уровни для модели; пусто — каталог не подтверждает выбор."""
    catalog = reasoning_catalog(agent_id) or {}
    spec = (catalog.get("models") or {}).get(model or "") or {}
    levels = spec.get("levels")
    return [str(item) for item in levels] if isinstance(levels, list) else []


def list_workers(at=None) -> list[dict]:
    moment = at or observations.now()
    with db._lock:
        conn = db.connect()
        rows = db._rows(conn, db._qualify(
            "select worker_id, host, os_user, pid, protocol, adapter_version, hb_interval_s,"
            " agents, capabilities, identity, started_at, hb_at from aihub.workers"
            " order by hb_at desc"))
    items = []
    for row in rows:
        agents = row.get("agents") if isinstance(row.get("agents"), list) else json.loads(row.get("agents") or "[]")
        capabilities = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else json.loads(row.get("capabilities") or "{}")
        items.append({
            "worker_id": row.get("worker_id"),
            "host": row.get("host"),
            "os_user": row.get("os_user"),
            "pid": row.get("pid"),
            "protocol": row.get("protocol"),
            "adapter_version": row.get("adapter_version"),
            "hb_interval_s": row.get("hb_interval_s"),
            "heartbeat_interval_s": recommended_heartbeat(LEASE_SECONDS),
            "lease_seconds": LEASE_SECONDS,
            "lease_risk": lease_risk(row.get("hb_interval_s"), LEASE_SECONDS),
            "agents": [item.get("id") for item in agents if isinstance(item, dict)],
            "agent_details": agents,
            "capabilities": capabilities,
            "state": state_of(row.get("hb_at"), moment),
            "hb_at": observations._iso(row.get("hb_at")),
            "age_s": observations.age_seconds(row.get("hb_at"), moment),
        })
    return items
