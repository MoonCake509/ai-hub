"""AI-Hub core agent (v0.3).

Асинхронная модель заданий по ревью архитектора (ARCH-001):
  * POST /agents/{id}/run  -> 202 + job_id, задание в Postgres, без удержания HTTP;
  * GET  /jobs/{id}        -> статус, попытки, результат;
  * POST /jobs/{id}/cancel -> отмена очереди или запрос отмены исполнения;
  * /bridge/*              -> pull-протокол воркеров: аренда, heartbeat, результат.
Автоматического перезапуска после потери аренды нет: попытка помечается unknown.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import ntpath
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import chat_context, db, observations, probes, quality, quality_db, quality_quick, workers
from . import reconciliation
from . import capabilities
from . import packets
from . import runs as runs_ctrl
from . import project_template
from . import templates as templates_ctrl
from . import protocol as bridge_protocol
from . import resources
from . import workspace
from .image_validation import validate_image

VERSION = "0.4.1"
_build_sha = os.environ.get("HUB_BUILD_SHA", "").strip().lower()
BUILD_SHA = _build_sha if re.fullmatch(r"[0-9a-f]{40,64}", _build_sha) else None
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
AGENTS_FILE = Path(os.environ.get("AGENTS_FILE", "/app/config/agents.yaml"))
WEB_DIR = Path(os.environ.get("WEB_DIR", "/app/web"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "300"))
# Пределы, чтобы хаб не превращался в бесконечную очередь и не хранил мегабайты вывода.
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "20000"))
MAX_QUEUE = int(os.environ.get("MAX_QUEUE", "50"))
MAX_RESULT_CHARS = int(os.environ.get("MAX_RESULT_CHARS", "100000"))
MIN_TIMEOUT_S = int(os.environ.get("MIN_TIMEOUT_S", "10"))
MAX_TIMEOUT_S = int(os.environ.get("MAX_TIMEOUT_S", "7200"))
# Сколько ждём подготовку папки проекта, прежде чем отдать карточку со статусом «failed».
PROJECT_INIT_WAIT_S = int(os.environ.get("PROJECT_INIT_WAIT_S", "90"))


# Период уборки аренд привязан к их длительности: при LEASE_SECONDS=3 минутный
# тик оставлял истёкшую аренду в running почти минуту, и поздний результат
# принимался как актуальный. Верхняя граница — минута.
SWEEP_INTERVAL_S = max(2, min(60, int(os.environ.get("LEASE_SECONDS", "300")) // 2))
# По расписанию карта сверяется целиком — но только по проектам без активной работы.
INDEX_SWEEP_S = int(os.environ.get("INDEX_SWEEP_S", "21600"))
_last_index_sweep = 0.0


async def _schedule_index_sweep() -> None:
    """Раз в INDEX_SWEEP_S обновить карту проектов, где нет активного прогона.

    Событие пропускается, пока по проекту идёт работа: карта строится по коммиту,
    а сверка во время правок дала бы ложную картину.
    """
    global _last_index_sweep
    now = time.monotonic()
    if _last_index_sweep and now - _last_index_sweep < INDEX_SWEEP_S:
        return
    _last_index_sweep = now
    stamp = time.strftime("%Y-%m-%d")
    for project in await asyncio.to_thread(db.list_projects):
        if project.get("status") != "ready" or not project.get("workdir"):
            continue
        runs = await asyncio.to_thread(runs_ctrl.list_runs, project.get("id"), 1)
        if runs and str(runs[0].get("state")) in {"active", "paused"}:
            continue
        try:
            await asyncio.to_thread(
                workspace.create_op_job, "refresh_index", {"root": project["workdir"]},
                str(project.get("host_id") or "laptop"), project.get("id"),
                f"autoindex:{project.get('id')}:{stamp}", 180)
        except Exception as exc:  # noqa: BLE001 — плановая сверка не должна ломать тик
            print(f"index sweep skipped for {project.get('id')}: {exc}", flush=True)


async def _sweeper() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            await _schedule_index_sweep()
            expired = await asyncio.to_thread(db.expire_leases)
            if expired:
                print(f"expired leases: {expired}", flush=True)
            # CORE-02: истёкшие аренды ресурсов освобождаются тем же тиком.
            await asyncio.to_thread(resources.sweep)
            # Quick-run deadlines are enforced even with no browser open.
            await asyncio.to_thread(quality.refresh_active_runs)
            await asyncio.to_thread(packets.poll_active)
            await asyncio.to_thread(runs_ctrl.poll_active, _resolve_run_agent)
        except Exception as exc:  # noqa: BLE001
            print(f"lease sweeper error: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.init)
    await asyncio.to_thread(quality_db.init)
    await asyncio.to_thread(observations.init)
    await asyncio.to_thread(workers.init)
    await asyncio.to_thread(resources.init)
    try:
        written = await asyncio.to_thread(
            observations.record_registry, load_agents().get("agents", []), None, VERSION)
        print(f"registry observations: {written}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print("registry observations failed:", exc, flush=True)
    load_profiles()
    try:
        db.seed_workspace()
    except Exception as e:
        print("seed_workspace failed:", e)
    task = asyncio.create_task(_sweeper())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="AI-Hub", version=VERSION, lifespan=lifespan)


def load_agents() -> dict:
    if not AGENTS_FILE.exists():
        return {"agents": []}
    with AGENTS_FILE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {"agents": []}


def find_agent(agent_id: str) -> dict | None:
    for item in load_agents().get("agents", []):
        if item.get("id") == agent_id:
            return item
    return None


def require_token(request: Request) -> None:
    if not HUB_TOKEN:
        return
    if request.headers.get("authorization", "") != f"Bearer {HUB_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing token")


@app.get("/health")
async def health() -> dict:
    try:
        await asyncio.to_thread(db.list_jobs, None, 1)
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        db_ok = str(exc)[:120]
    return {"status": "ok", "service": "hub-agent", "version": VERSION,
            "build_sha": BUILD_SHA, "db": db_ok}


def with_observations(item: dict, summary: dict | None = None) -> dict:
    """Карточка агента вместе с наблюдениями (DIAG-01).

    Поле status остаётся для совместимости, но считается из configured и
    compatibility, а не берётся из YAML как есть.
    """
    data = dict(item)
    data.setdefault("model_selection_reason", None)
    data.setdefault("supports_workdir", False)
    observed = summary if summary is not None else observations.evaluate_one(
        data, None, VERSION)
    data["status"] = observed.get("status") or data.get("status")
    data["observations"] = observed.get("observations") or {}
    data["effective_state"] = observed.get("effective_state", "unknown")
    data["effective_reason"] = observed.get("effective_reason")
    data["observations_checked_at"] = observed.get("checked_at")
    return data


@app.get("/agents")
def agents() -> dict:
    raw_agents = load_agents().get("agents", [])
    summary = observations.evaluate(raw_agents, None, VERSION)
    items = [with_observations(raw, summary.get(str(raw.get("id") or "")))
             for raw in raw_agents]
    return {"count": len(items), "agents": items}


# Профили провайдеров и модели для выбора в интерфейсе. Источник — реестр агентов,
# а не внешние платные вызовы: хаб не расходует бюджет на перечисление каталога.
AGENT_PROFILES: dict[str, dict] = {}


def load_profiles() -> None:
    for item in load_agents().get("agents", []):
        AGENT_PROFILES[item["id"]] = {
            "agent_id": item["id"],
            "model_selection": bool(item.get("model_selection")),
            "model_selection_reason": item.get("model_selection_reason"),
            "profiles": item.get("profiles") or [],
            "default_profile": item.get("default_profile") or "",
        }


@app.get("/agent-profiles/{agent_id}", dependencies=[Depends(require_token)])
async def agent_profiles(agent_id: str) -> dict:
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    prof = AGENT_PROFILES.get(agent_id) or {"profiles": [], "default_profile": ""}
    observed = await asyncio.to_thread(observations.evaluate_one, item, None, VERSION)
    return {
        "agent_id": agent_id,
        "observations": observed.get("observations") or {},
        "effective_state": observed.get("effective_state", "unknown"),
        "effective_reason": observed.get("effective_reason"),
        "observations_checked_at": observed.get("checked_at"),
        "model_selection": bool(item.get("model_selection")),
        "model_selection_reason": item.get("model_selection_reason"),
        "input_modalities": item.get("input_modalities", ["text"]),
        "output_modalities": item.get("output_modalities", ["text"]),
        "required_modalities": item.get("required_modalities", []),
        "context_mode": item.get("context_mode"),
        "filesystem_access": item.get("filesystem_access"),
        "supports_workdir": bool(item.get("supports_workdir")),
        "requires_image": bool(item.get("requires_image")),
        # ADAPTER-01: что канал действительно умеет — с причиной для выключенного.
        "capabilities": await asyncio.to_thread(capabilities.describe, item),
        **prof,
    }


@app.get("/agents/{agent_id}/capabilities", dependencies=[Depends(require_token)])
async def agent_capabilities(agent_id: str, profile: str | None = None,
                             model: str | None = None) -> dict:
    """Матрица возможностей канала: что подтверждено, что только заявлено."""
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    try:
        if profile or model:
            return await asyncio.to_thread(capabilities.resolve, item, profile, model)
        return await asyncio.to_thread(capabilities.describe, item)
    except capabilities.CapabilityError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None


QUALITY_MIN_TIMEOUT_S = 10
QUALITY_MAX_TIMEOUT_S = 180


def _quality_text(payload: dict, key: str, default: str | None = None, required: bool = False) -> str | None:
    value = payload.get(key, default)
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail=f"{key} is required")
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise HTTPException(status_code=400, detail=f"{key} is required")
    return value or None


def _resolve_quality_channel(agent_id: str, profile: str | None, model: str | None) -> tuple[dict, str | None, str | None]:
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    if not item.get("bridge_agent"):
        raise HTTPException(status_code=501, detail=f"adapter for '{agent_id}' is not implemented yet")
    if not item.get("model_selection"):
        raise HTTPException(status_code=400, detail="quality runs require an agent with selectable provider/model")
    if profile is not None and not isinstance(profile, str):
        raise HTTPException(status_code=400, detail="profile must be a string")
    if model is not None and not isinstance(model, str):
        raise HTTPException(status_code=400, detail="model must be a string")
    try:
        resolved_profile, resolved_model = _validate_chat_profile(
            item, agent_id, (profile or "").strip(), (model or "").strip() or None)
    except HTTPException:
        raise
    return item, resolved_profile or None, resolved_model or None


@app.get("/quality/suites", dependencies=[Depends(require_token)])
async def quality_suites() -> dict:
    return {"suites": quality.list_suites()}


@app.get("/quality/channels", dependencies=[Depends(require_token)])
async def quality_channels() -> dict:
    return {"channels": quality.channel_catalog(AGENT_PROFILES, load_agents().get("agents", []))}


@app.patch("/quality/channels", dependencies=[Depends(require_token)])
async def quality_update_channel(request: Request) -> dict:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request must be a JSON object")
    enabled = payload.get("enabled")
    reason = _quality_text(payload, "reason", required=True)
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be true or false")
    if not reason or len(reason) > 500:
        raise HTTPException(status_code=400, detail="reason is required (1..500 characters)")
    channel_id = _quality_text(payload, "channel_id")
    requested_agent = _quality_text(payload, "agent_id")
    if channel_id:
        available = quality.channel_catalog(AGENT_PROFILES, load_agents().get("agents", []))
        target = next((item for item in available if item["channel_id"] == channel_id), None)
    elif requested_agent:
        item, profile, model = _resolve_quality_channel(
            requested_agent, payload.get("profile"), payload.get("model"))
        target = {
            "channel_id": quality.channel_id(requested_agent, profile, model),
            "agent_id": requested_agent, "profile": profile, "model": model,
        }
    else:
        raise HTTPException(status_code=400, detail="channel_id or agent_id/profile/model is required")
    if not target:
        raise HTTPException(status_code=404, detail="channel not found in the current provider/model catalog")
    state = await asyncio.to_thread(quality_db.set_channel, target, enabled, reason)
    audit = await asyncio.to_thread(quality_db.channel_audit, 100)
    return {"channel": state, "audit": [item for item in audit if item.get("channel_id") == target["channel_id"]]}


def _ensure_quality_project() -> None:
    """Create the internal owner for synthetic quality attachments."""
    if db.get_project("_quality"):
        return
    # create_project seeds default roles; they are not useful for an internal
    # owner and would block the stable id. Remove them in the same transaction.
    project = db.create_project("Системные проверки", "hub", "", {"internal": True})
    with db._lock:
        conn = db.connect()
        conn.run("begin")
        try:
            conn.run(db._qualify("delete from aihub.roles where project_id=:i"), i=project["id"])
            conn.run(db._qualify("update aihub.projects set id='_quality' where id=:i"), i=project["id"])
            conn.run("commit")
        except Exception:
            conn.run("rollback")
            raise
    if not db.get_project("_quality"):
        raise RuntimeError("internal quality project could not be created")


@app.post("/quality/runs", dependencies=[Depends(require_token)])
async def quality_start_run(request: Request) -> JSONResponse:
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request must be a JSON object")
    mode = _quality_text(payload, "mode", "extended") or "extended"
    if mode not in ("quick", "extended"):
        raise HTTPException(status_code=400, detail="mode must be quick or extended")
    agent_id = _quality_text(payload, "agent_id", required=True)
    item, profile, model = _resolve_quality_channel(agent_id, payload.get("profile"), payload.get("model"))
    idem = payload.get("idempotency_key")
    if not isinstance(idem, str) or not idem.strip() or len(idem.strip()) > 200:
        raise HTTPException(status_code=400, detail="idempotency_key must be 1..200 characters")
    idem = idem.strip()
    # Replay is purely a read: it must not depend on current capabilities or
    # on whether the channel now requires an image.
    existing = await asyncio.to_thread(quality_db.get_run_by_key, idem)
    if existing:
        channel = quality.channel_state(agent_id, profile, model)
        same = (existing.get("channel_id") == channel["channel_id"]
                and [row.get("case_id") for row in existing.get("items", [])]
                == [row.get("case_id") for row in existing.get("items", [])])
        if not same:
            raise HTTPException(status_code=409, detail="idempotency_key already used with a different quality run")
        refreshed = await asyncio.to_thread(quality.refresh_run, existing["id"])
        return JSONResponse(status_code=200, content=jsonable_encoder(quality.public_run(refreshed)))
    capabilities = _model_capabilities(item, agent_id, profile or "", model)
    requires_image = bool(capabilities.get("requires_image")
                          or "image" in set(capabilities.get("required_modalities") or []))
    if mode == "quick":
        if payload.get("case_count") is not None or payload.get("case_ids") is not None:
            raise HTTPException(status_code=400, detail="quick mode selects its own bounded probe set")
        suite = quality_quick.build_suite(idem, requires_image)
        cases = suite["cases"]
        case_count = len(cases)
        try:
            budget_s = quality_quick.normalize_budget(payload.get("budget_s"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # The common wall budget is not a multiplication of a per-job timeout.
        # An image-only channel gets exactly one call; a text channel at most 3.
        timeout_s = budget_s
    else:
        suite_id = _quality_text(payload, "suite_id", quality.SUITE_ID) or quality.SUITE_ID
        suite_version = _quality_text(payload, "suite_version", quality.SUITE_VERSION) or quality.SUITE_VERSION
        suite = quality.get_suite(suite_id, suite_version)
        if suite is None:
            raise HTTPException(status_code=400, detail="unknown suite or suite version")
        try:
            case_count = payload.get("case_count")
            if isinstance(case_count, str) and case_count.isdigit():
                case_count = int(case_count)
            cases = quality.select_cases(case_count, payload.get("case_ids"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if requires_image:
            raise HTTPException(status_code=400,
                                detail="The extended quality suite is text-only; use quick mode for an OCR channel.")
        try:
            timeout_s = int(payload.get("timeout_s", 60))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="timeout_s must be an integer") from None
        if not QUALITY_MIN_TIMEOUT_S <= timeout_s <= QUALITY_MAX_TIMEOUT_S:
            raise HTTPException(status_code=400,
                                detail=f"timeout_s must be between {QUALITY_MIN_TIMEOUT_S} and {QUALITY_MAX_TIMEOUT_S}")
        budget_s = None

    channel = quality.channel_state(agent_id, profile, model)
    run_data = {
        "idempotency_key": idem,
        "suite_id": suite["id"], "suite_version": suite["version"],
        "grader_version": suite["grader_version"], "suite_snapshot": suite,
        "channel_id": channel["channel_id"], "agent_id": agent_id,
        "profile": profile, "model": model, "case_count": case_count,
        "timeout_s": timeout_s, "mode": mode, "budget_s": budget_s,
        "max_calls": len(cases),
    }
    try:
        quality.ensure_channel_enabled(agent_id, profile, model)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    snapshot_fixture = None
    if requires_image:
        # The fixture is content-addressed in a dedicated internal project.
        # It never enters a user conversation or the host filesystem.
        await asyncio.to_thread(_ensure_quality_project)
        fixture = quality_quick.ocr_fixture_png()
        metadata = validate_image(fixture, "image/png", "quick-ocr.png")
        att = await asyncio.to_thread(db.create_attachment, "_quality", "upload", "quick-ocr.png",
                                      "image/png", fixture, None, None, metadata)
        snapshot_fixture = {"id": att["id"], "version": att["id"], "filename": att["filename"],
                            "mime": "image/png", "source": "upload", "project_id": "_quality"}

    run, created = await asyncio.to_thread(quality_db.create_run, run_data, cases)
    if not created:
        existing_cases = [row.get("case_id") for row in run.get("items", [])]
        if (run.get("channel_id") != channel["channel_id"] or run.get("suite_version") != suite["version"]
                or run.get("timeout_s") != timeout_s or (run.get("mode") or "extended") != mode
                or existing_cases != [case["id"] for case in cases]):
            raise HTTPException(status_code=409, detail="idempotency_key already used with a different quality run")
        return JSONResponse(status_code=200, content=jsonable_encoder(quality.public_run(run)))

    snapshot = {"quality_run_id": run["id"], "suite_id": suite["id"],
                "suite_version": suite["version"], "grader_version": suite["grader_version"],
                "quality_mode": mode}
    if snapshot_fixture:
        snapshot.update({"project_id": "_quality", "project_name": "Системные проверки",
                         "host_id": item.get("host"), "workdir": None,
                         "attachment_ids": [snapshot_fixture["id"]],
                         "attachment_versions": {snapshot_fixture["id"]: snapshot_fixture["id"]},
                         "attachment_refs": [snapshot_fixture],
                         "input_modalities": capabilities.get("input_modalities") or ["text", "image"],
                         "output_modalities": capabilities.get("output_modalities") or ["text"],
                         "required_modalities": ["image"], "context_mode": "image",
                         "task_text": cases[0]["prompt"]})
    items_by_case = {row["case_id"]: row for row in run.get("items", [])}
    enqueue_error = None
    for case in cases:
        row = items_by_case[case["id"]]
        try:
            quality.ensure_channel_enabled(agent_id, profile, model)
            job_snapshot = {**snapshot, "quality_case_id": case["id"],
                            "quality_case_snapshot": case, "agent_id": agent_id,
                            "provider_profile": profile, "requested_model": model,
                            "identity_evidence_source": "not_reported_until_adapter_returns_metadata"}
            prompt_sha = hashlib.sha256(case["prompt"].encode("utf-8")).hexdigest()
            job, _ = await asyncio.to_thread(
                db.create_job, item.get("bridge_agent") or agent_id, case["prompt"], prompt_sha,
                timeout_s, f"quality:{run['id']}:{case['id']}", MAX_QUEUE, model, profile, job_snapshot)
            await asyncio.to_thread(quality_db.attach_job, row["id"], job)
            await asyncio.to_thread(quality.refresh_job, job["id"])
        except Exception as exc:  # noqa: BLE001
            enqueue_error = str(exc)
            for remaining in cases[cases.index(case):]:
                await asyncio.to_thread(quality_db.mark_item_enqueue_error,
                                        items_by_case[remaining["id"]]["id"], enqueue_error)
            await asyncio.to_thread(quality_db.set_run_error, run["id"], enqueue_error)
            break
    refreshed = await asyncio.to_thread(quality.refresh_run, run["id"])
    return JSONResponse(status_code=202, content=jsonable_encoder(quality.public_run(refreshed)))


@app.get("/quality/runs", dependencies=[Depends(require_token)])
async def quality_runs(limit: int = 30, offset: int = 0) -> dict:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    await asyncio.to_thread(quality.refresh_active_runs)
    rows = await asyncio.to_thread(quality_db.list_runs, limit, offset)
    return {"count": len(rows), "limit": limit, "offset": offset,
            "runs": [quality.public_run(row, include_items=False) for row in rows]}


@app.get("/quality/runs/{run_id}", dependencies=[Depends(require_token)])
async def quality_run(run_id: str) -> dict:
    data = await asyncio.to_thread(quality.refresh_run, run_id)
    if not data:
        raise HTTPException(status_code=404, detail="quality run not found")
    return quality.public_run(data)


@app.get("/agents/{agent_id}")
def agent(agent_id: str) -> dict:
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    return with_observations(item)


@app.get("/probes/plan", dependencies=[Depends(require_token)])
def probes_plan() -> dict:
    config = load_agents()
    items = probes.plan(config.get("agents", []), config.get("services"))
    return {"count": len(items),
            "items": [{key: value for key, value in item.items() if key != "probe"} for item in items]}


@app.post("/probes/run", dependencies=[Depends(require_token)])
async def probes_run(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — тело необязательно
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request must be a JSON object")
    agent_id = body.get("agent_id")
    if agent_id is not None and not isinstance(agent_id, str):
        raise HTTPException(status_code=400, detail="agent_id must be a string")
    force = bool(body.get("force"))
    config = load_agents()
    result = await asyncio.to_thread(probes.run, config.get("agents", []), agent_id, force,
                                     None, config.get("services"))
    if agent_id and not result["results"]:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' has no probe")
    return result


@app.get("/probes/last", dependencies=[Depends(require_token)])
def probes_last() -> dict:
    return probes.last()


@app.post("/agents/{agent_id}/run", dependencies=[Depends(require_token)])
async def run_agent(agent_id: str, request: Request) -> JSONResponse:
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    bridge_agent = item.get("bridge_agent")
    if not bridge_agent:
        raise HTTPException(status_code=501, detail=f"adapter for '{agent_id}' is not implemented yet")

    payload = await request.json()
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(
            status_code=413,
            detail=f"prompt too long: {len(prompt)} > {MAX_PROMPT_CHARS} chars")
    try:
        raw_timeout = payload.get("timeout")
        timeout_s = int(600 if raw_timeout is None else raw_timeout)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout must be an integer") from None
    if not (MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S):
        raise HTTPException(status_code=400,
                            detail=f"timeout out of range: {timeout_s} not in [{MIN_TIMEOUT_S}, {MAX_TIMEOUT_S}]")
    idem = payload.get("idempotency_key") or request.headers.get("idempotency-key")
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    # Выбор провайдера/модели: реальная конфигурация, а не только поле в интерфейсе.
    # Несовместимое сочетание отклоняем до постановки в очередь.
    requested_model = (payload.get("model") or "").strip() or None
    requested_profile = (payload.get("profile") or "").strip() or None
    # Адаптер должен уметь применять выбор; иначе явный запрос — ошибка, а не тихое игнорирование.
    if (requested_model or requested_profile) and not item.get("model_selection"):
        raise HTTPException(
            status_code=400,
            detail=f"agent '{agent_id}' does not support model selection yet; "
                   "адаптер пока игнорирует выбор провайдера/модели")
    prof = AGENT_PROFILES.get(agent_id) or {}
    known_profiles = prof.get("profiles") or []
    default_profile = prof.get("default_profile") or ""
    if not requested_profile:
        requested_profile = default_profile
    profile_spec = None
    for p in known_profiles:
        if p.get("id") == requested_profile:
            profile_spec = p
            break
    if requested_profile and profile_spec is None:
        raise HTTPException(status_code=400,
                            detail=f"unknown provider profile '{requested_profile}' for agent '{agent_id}'")
    if profile_spec is not None:
        known_models = profile_spec.get("models") or []
        if requested_model and requested_model not in known_models:
            raise HTTPException(status_code=400,
                                detail=f"model '{requested_model}' not supported by profile '{requested_profile}' "
                                       f"of agent '{agent_id}'. Supported: {', '.join(known_models) or 'none'}")
        if not requested_model and known_models:
            requested_model = known_models[0]

    # ADAPTER-01: проверка требуемых возможностей до постановки в очередь.
    # Неподдержанный режим отклоняется с причиной, а не исполняется «как получится».
    needs = payload.get("needs") or []
    if not isinstance(needs, list):
        raise HTTPException(status_code=400, detail="needs must be a list of capability names")
    try:
        cap_snapshot = await asyncio.to_thread(
            capabilities.preflight, item, requested_profile, requested_model, needs)
    except capabilities.CapabilityError as exc:
        raise HTTPException(status_code=409, detail=exc.as_dict()) from None

    # Return an accepted idempotent job even after its channel is disabled.
    if idem:
        existing = await asyncio.to_thread(db.get_job_by_idempotency_key, idem)
        if existing:
            try:
                job, created = await asyncio.to_thread(
                    db.create_job, bridge_agent, prompt, prompt_sha, timeout_s, idem, MAX_QUEUE,
                    requested_model, requested_profile)
            except ValueError:
                raise HTTPException(status_code=409, detail="idempotency_key already used with another payload") from None
            return JSONResponse(status_code=202, content={
                "job_id": job["id"], "status": job["status"], "created": created, "agent": agent_id,
                "capabilities": cap_snapshot["capabilities"],
            })
    try:
        quality.ensure_channel_enabled(agent_id, requested_profile, requested_model)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    try:
        job, created = await asyncio.to_thread(
            db.create_job, bridge_agent, prompt, prompt_sha, timeout_s, idem or None, MAX_QUEUE,
            requested_model, requested_profile)
    except ValueError:
        raise HTTPException(status_code=409, detail="idempotency_key already used with another payload") from None
    except db.QueueFull as exc:
        raise HTTPException(
            status_code=429,
            detail=f"queue is full ({exc}); повтори позже или дождись исполнения текущих заданий",
            headers={"Retry-After": "30"}) from None

    return JSONResponse(status_code=202, content={
        "job_id": job["id"],
        "status": job["status"],
        "created": created,
        "agent": agent_id,
        "capabilities": cap_snapshot["capabilities"],
    })


@app.get("/jobs", dependencies=[Depends(require_token)])
async def jobs(status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    items = await asyncio.to_thread(db.list_jobs, status, limit, offset)
    total = await asyncio.to_thread(db.count_jobs, status)
    return {"count": len(items), "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(items) < total, "jobs": items}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
async def job(job_id: str) -> dict:
    data = await asyncio.to_thread(db.get_job, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job not found")
    return data


@app.get("/late-results", dependencies=[Depends(require_token)])
async def late_results(limit: int = 50) -> dict:
    items = await asyncio.to_thread(db.list_late, max(1, min(limit, 200)))
    return {"count": len(items), "items": items}


@app.get("/jobs/{job_id}/events", dependencies=[Depends(require_token)])
async def job_events(job_id: str) -> dict:
    data = await asyncio.to_thread(db.get_job, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job not found")
    items = await asyncio.to_thread(db.events, job_id)
    return {"job_id": job_id, "events": items}



# ---------- 9.2: рабочее пространство проекта ----------

TEXT_MIME_OK = {"text/plain", "text/markdown", "text/csv", "text/html", "application/json",
                "application/yaml", "text/yaml", "application/xml", "text/xml",
                "application/javascript", "text/javascript", "text/css", "text/x-python",
                "application/x-python-code", "application/x-sh", "text/x-shellscript",
                "application/toml", "text/x-toml", "text/x-ini", "application/sql", "text/sql",
                "application/typescript", "application/x-javascript", "application/x-yaml",
                "application/x-toml", "application/x-python", "application/x-sh"}
MAX_ATTACH_BYTES = 2_000_000
ALLOWED_EXT = (".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml", ".html",
               ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".sql", ".toml",
               ".ini", ".xml", ".sh", ".ps1")
IMAGE_EXT = {".png", ".jpg", ".jpeg"}
IMAGE_MIME = {"image/png", "image/jpeg"}


@app.get("/projects", dependencies=[Depends(require_token)])
async def projects() -> dict:
    items = await asyncio.to_thread(db.list_projects)
    return {"projects": items}


@app.get("/projects/{project_id}", dependencies=[Depends(require_token)])
async def project(project_id: str) -> dict:
    data = await asyncio.to_thread(db.get_project, project_id)
    if not data:
        raise HTTPException(status_code=404, detail="project not found")
    return data


def _project_dir_name(name: str, project_id: str) -> str:
    """Имя папки: безопасная часть названия и короткий ID, чтобы одноимённые не слипались."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "project"
    return f"{slug}-{project_id[:8]}"


# Статусы, после которых задание воркера уже не изменится.
JOB_TERMINAL = {"succeeded", "done", "failed", "cancelled", "unknown", "error"}
JOB_SUCCESS = {"succeeded", "done"}


def _wait_workspace_job(job_id: str, timeout_s: int) -> dict | None:
    deadline = time.monotonic() + max(5, timeout_s)
    while time.monotonic() < deadline:
        result = workspace.job_result(job_id)
        if result and result.get("status") in JOB_TERMINAL:
            return result
        time.sleep(0.5)
    return workspace.job_result(job_id)


async def _prepare_project(project: dict, name: str, host_id: str, settings: dict) -> dict:
    """Поставить подготовку папки на хост и дождаться результата.

    Повтор безопасен: воркер не перезаписывает существующие файлы и не делает
    второй коммит, поэтому тот же вызов завершает прерванную подготовку.
    """
    project_id = str(project["id"])
    workdir = str(project.get("workdir") or "").strip()
    if not workdir:
        workdir = ntpath.join(await asyncio.to_thread(workspace.projects_root),
                              _project_dir_name(name, project_id))
    branch = str(settings.get("branch") or "main")
    args = {
        "root": workdir,
        "name": name[:200],
        "branch": branch,
        "author_name": str(settings.get("author_name") or "AI Hub"),
        "author_email": str(settings.get("author_email") or "aihub@local"),
        "repo_subdir": project_template.REPO_SUBDIR,
        "template_version": project_template.TEMPLATE_VERSION,
        "template_files": project_template.starter_files(name[:200], workdir, branch, project_id),
    }
    try:
        job = await asyncio.to_thread(
            workspace.create_op_job, "init_project", args, host_id, project_id,
            f"init:{project_id}", 180)
    except workspace.OpError as exc:
        # Карточка остаётся, причина отказа — в ней: папку никто не создавал.
        error = {"code": exc.code, "message": str(exc)}
        await asyncio.to_thread(db.project_prepare_failed, project_id, error)
        project = await asyncio.to_thread(db.get_project, project_id)
        project["prepare_error"] = error
        return project

    result = await asyncio.to_thread(_wait_workspace_job, job["job"]["id"], PROJECT_INIT_WAIT_S)
    data = (result or {}).get("result")
    if (result or {}).get("status") in JOB_SUCCESS and isinstance(data, dict):
        project = await asyncio.to_thread(
            db.project_prepared, project_id,
            repo_root=data.get("root") or workdir,
            git_root=data.get("git_root") or workdir,
            branch=data.get("branch") or branch,
            head_sha=data.get("head_sha") or "",
            template_version=data.get("template_version") or project_template.TEMPLATE_VERSION)
        project["prepare_job"] = job["job"]["id"]
        return project

    error = {
        "code": "prepare_failed",
        "message": (result or {}).get("error") or "project preparation did not finish",
        "status": (result or {}).get("status"),
        "job_id": job["job"]["id"],
    }
    await asyncio.to_thread(db.project_prepare_failed, project_id, error)
    project = await asyncio.to_thread(db.get_project, project_id)
    project["prepare_job"] = job["job"]["id"]
    project["prepare_error"] = error
    return project


def _ensure_restore_point(project: dict, label: str) -> dict:
    """Точка восстановления до старта работы, если проект её требует.

    Кодер не стартует без подтверждённой точки: задание на бэкап создаётся
    хабом, ждём его отчёт с контрольной суммой. Отчёт с ошибкой — отказ старта.
    """
    settings = project.get("settings") if isinstance(project.get("settings"), dict) else {}
    if not settings.get("backup_required"):
        return {"ok": True, "skipped": "backup not required"}
    root = str(project.get("workdir") or "").strip()
    if not root:
        return {"ok": False, "error": "project has no workdir"}
    job = workspace.create_op_job(
        "create_backup", {"root": root, "label": label,
                          "host": str(project.get("host_id") or "laptop")},
        str(project.get("host_id") or "laptop"), str(project.get("id") or ""),
        f"backup:{project.get('id')}:{project.get('head_sha') or ''}:{label}", 180)
    job_id = job["job"]["id"]
    final = _wait_workspace_job(job_id, int(os.environ.get("BACKUP_WAIT_S", "180")))
    if final.get("status") != "succeeded":
        return {"ok": False, "error": final.get("error") or "backup job failed", "job": job_id}
    report = final.get("result")
    if not isinstance(report, dict):
        try:
            report = json.loads(final.get("result_output") or "{}")
        except (TypeError, ValueError):
            return {"ok": False, "error": "backup report is not json", "job": job_id}
    if not report.get("sha256") or not report.get("path"):
        return {"ok": False, "error": "backup report has no checksum", "job": job_id}
    return {"ok": True, "job": job_id, "path": report.get("path"),
            "sha256": report.get("sha256"), "head_sha": report.get("head_sha")}


@app.post("/projects/{project_id}/backups", dependencies=[Depends(require_token)])
def create_project_backup(project_id: str, label: str = "manual") -> dict:
    """Поставить точку восстановления вручную и дописать журнал ops/BACKUPS.md."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    result = _ensure_restore_point({**project, "settings": {"backup_required": True}}, label)
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result)
    return result


@app.post("/projects/{project_id}/index/refresh", dependencies=[Depends(require_token)])
def refresh_project_index(project_id: str) -> dict:
    """Архивариус: карта проекта по зафиксированному коммиту, отметка в карточке."""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    root = str(project.get("workdir") or "").strip()
    if not root:
        raise HTTPException(status_code=409, detail={"code": "no_workdir"})
    try:
        job = workspace.create_op_job(
            "refresh_index", {"root": root}, str(project.get("host_id") or "laptop"),
            project_id, None, 180)
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    job_id = job["job"]["id"]
    final = _wait_workspace_job(job_id, int(os.environ.get("INDEX_WAIT_S", "180")))
    if final.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail={"code": "index_failed",
                                                     "message": final.get("error"), "job": job_id})
    report = final.get("result") if isinstance(final.get("result"), dict) else {}
    if not report.get("digest"):
        raise HTTPException(status_code=409, detail={"code": "index_report_invalid", "job": job_id})
    db.project_index_updated(project_id, str(report.get("head_sha") or ""), int(report.get("files") or 0))
    return {"job": job_id, **report}


async def _project_op(project_id: str, op: str, args: dict, wait_s: int) -> dict:
    """Запустить операцию воркера по проекту и дождаться её отчёта."""
    project = await asyncio.to_thread(db.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    root = str(project.get("workdir") or "").strip()
    if not root:
        raise HTTPException(status_code=409, detail={"code": "no_workdir"})
    try:
        job = await asyncio.to_thread(
            workspace.create_op_job, op, {**args, "root": root},
            str(project.get("host_id") or "laptop"), project_id, None, wait_s)
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    job_id = job["job"]["id"]
    final = await asyncio.to_thread(_wait_workspace_job, job_id, wait_s)
    if final.get("status") != "succeeded":
        raise HTTPException(status_code=409, detail={
            "code": "op_failed", "message": final.get("error"), "job": job_id})
    return {"job": job_id, **(final.get("result") if isinstance(final.get("result"), dict) else {})}


@app.get("/projects/{project_id}/search", dependencies=[Depends(require_token)])
async def search_project(project_id: str, q: str = "", limit: int = 50) -> dict:
    """Поиск по именам и строкам коммита: путь, строка, версия файла (SHA-256)."""
    result = await _project_op(project_id, "search_git",
                               {"q": q, "limit": max(1, min(100, int(limit)))}, 120)
    return result


@app.get("/projects/{project_id}/index/file", dependencies=[Depends(require_token)])
async def project_file_at(project_id: str, path: str = "", sha256: str = "") -> dict:
    """Фрагмент файла из коммита; при несовпадении версии — отказ, а не чужое содержимое."""
    result = await _project_op(project_id, "read_at", {"path": path, "sha256": sha256}, 120)
    return result


@app.post("/projects/{project_id}/index/describe", dependencies=[Depends(require_token)])
async def describe_project_files(project_id: str, limit: int = 20) -> dict:
    """Описания файлов локальной моделью; при выключенной модели карта не страдает."""
    result = await _project_op(project_id, "describe_files",
                               {"limit": max(1, min(50, int(limit)))}, 300)
    return result


@app.post("/projects/{project_id}/reports/sync", dependencies=[Depends(require_token)])
def sync_project_reports(project_id: str, run_id: str | None = None) -> dict:
    """Собрать ops/TASKFLOW.md и черновики PR по прогону и записать их на хост."""
    from . import reports

    try:
        result = reports.sync_project(project_id, run_id)
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error") or "not found")
    return result


@app.post("/projects", dependencies=[Depends(require_token)])
async def project_create(request: Request) -> dict:
    payload = await request.json()
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    host_id = (payload.get("host_id") or "laptop").strip()
    settings = payload.get("settings") or {}
    if not isinstance(settings, dict):
        raise HTTPException(status_code=400, detail="settings must be an object")
    key = (payload.get("idempotency_key") or "").strip()[:200]

    if key:
        existing = await asyncio.to_thread(db.project_by_request_key, key)
        if existing:
            if existing.get("status") == "failed":
                # Повтор прерванной или неудачной подготовки: тот же проект и та же папка.
                return await _prepare_project(existing, str(existing.get("name") or name),
                                              str(existing.get("host_id") or host_id),
                                              existing.get("settings") or settings)
            return existing

    project_id = uuid.uuid4().hex
    root = await asyncio.to_thread(workspace.projects_root)
    workdir = ntpath.join(root, _project_dir_name(name, project_id))
    project = await asyncio.to_thread(
        db.create_project, name[:200], host_id, workdir, settings,
        status="preparing", request_key=key or None, project_id=project_id)
    return await _prepare_project(project, name, host_id, settings)


@app.post("/projects/import", dependencies=[Depends(require_token)])
async def project_import(request: Request) -> dict:
    """Подключить существующий репозиторий: фактический корень, ветка и HEAD.

    Файлы не перезаписываются и история не меняется: воркер только читает
    состояние каталога, карточка получает фактические пути и коммит.
    """
    payload = await request.json()
    path = str(payload.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if not ntpath.isabs(path):
        raise HTTPException(status_code=400, detail="path must be absolute")
    host_id = str(payload.get("host_id") or "laptop").strip()
    name = str(payload.get("name") or ntpath.basename(path.rstrip("\\/")) or "Repository")[:200]
    key = str(payload.get("idempotency_key") or "").strip()[:200]
    if key:
        existing = await asyncio.to_thread(db.project_by_request_key, key)
        if existing:
            return existing
    try:
        job = await asyncio.to_thread(
            workspace.create_op_job, "inspect", {"root": path}, host_id, None, None, 120)
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    final = await asyncio.to_thread(_wait_workspace_job, job["job"]["id"], 120)
    report = final.get("result") if isinstance(final.get("result"), dict) else {}
    if final.get("status") not in JOB_SUCCESS:
        raise HTTPException(status_code=409, detail={"code": "inspect_failed",
                                                     "message": final.get("error")})
    if not report.get("is_git"):
        raise HTTPException(status_code=409, detail={"code": "not_git",
                                                     "message": "в каталоге нет Git-репозитория"})
    project = await asyncio.to_thread(
        db.create_project, name, host_id, path, {"imported": True},
        status="preparing", request_key=key or None)
    project = await asyncio.to_thread(
        db.project_prepared, project["id"],
        repo_root=report.get("root") or path,
        git_root=report.get("root") or path,
        branch=report.get("branch") or "main",
        head_sha=report.get("head") or "",
        template_version="import")
    project["inspect"] = {k: report.get(k) for k in
                          ("files", "bytes", "dirty_entries", "tree_digest", "host")}
    return project


@app.patch("/projects/{project_id}", dependencies=[Depends(require_token)])
async def project_update(project_id: str, request: Request) -> dict:
    payload = await request.json()
    data = await asyncio.to_thread(
        db.update_project, project_id,
        payload.get("name"), payload.get("host_id"), payload.get("workdir"),
        payload.get("settings"))
    if not data:
        raise HTTPException(status_code=404, detail="project not found")
    return data


@app.patch("/projects/{project_id}/roles/{role_id}", dependencies=[Depends(require_token)])
async def role_update(project_id: str, role_id: str, request: Request) -> dict:
    payload = await request.json()
    if not await asyncio.to_thread(db.get_project, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    agent_id = payload.get("agent_id")
    existing_role = await asyncio.to_thread(db.get_role, project_id, role_id)
    agent_changed = False
    if agent_id is not None:
        if not str(agent_id).strip():
            raise HTTPException(status_code=400, detail="agent_id cannot be empty")
        agent = find_agent(str(agent_id).strip())
        if agent is None:
            raise HTTPException(status_code=400, detail=f"agent '{agent_id}' not found")
        agent_changed = bool(existing_role and str(agent_id).strip() != existing_role.get("agent_id"))
        if agent_changed and not agent.get("model_selection"):
            # UI forms may submit the prior agent's values alongside the new agent.
            # Discard them, then persist SQL NULL for both fields below.
            payload["profile"] = ""
            payload["model"] = ""
        elif (payload.get("profile") or payload.get("model")) and not agent.get("model_selection"):
            raise HTTPException(status_code=400, detail=f"agent '{agent_id}' does not support profile/model selection")
        if agent_changed:
            payload.setdefault("profile", "")
            payload.setdefault("model", "")
    if payload.get("profile") and "model" not in payload:
        # Changing profiles must not leave an old model selected under the new profile.
        payload["model"] = ""
    if payload.get("profile") or payload.get("model"):
        selected_agent = find_agent(str(agent_id or (await asyncio.to_thread(db.get_role, project_id, role_id) or {}).get("agent_id", "")))
        if not selected_agent or not selected_agent.get("model_selection"):
            raise HTTPException(status_code=400, detail="selected agent does not support profile/model selection")
        prof = AGENT_PROFILES.get(selected_agent["id"]) or {}
        profile = payload.get("profile") or prof.get("default_profile") or ""
        spec = next((p for p in prof.get("profiles", []) if p.get("id") == profile), None)
        if profile and spec is None:
            raise HTTPException(status_code=400, detail=f"unknown provider profile '{profile}'")
        if payload.get("model") and (not spec or payload["model"] not in (spec.get("models") or [])):
            raise HTTPException(status_code=400, detail=f"model '{payload['model']}' is not available for profile '{profile}'")
    if payload.get("timeout_s") is not None:
        try:
            role_timeout = int(payload["timeout_s"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="timeout_s must be an integer") from None
        if not (MIN_TIMEOUT_S <= role_timeout <= MAX_TIMEOUT_S):
            raise HTTPException(status_code=400, detail="timeout_s out of range")
        payload["timeout_s"] = role_timeout
    if payload.get("model") == "":
        await asyncio.to_thread(db.clear_role_model, project_id, role_id)
        payload = {k: v for k, v in payload.items() if k != "model"}
    try:
        data = await asyncio.to_thread(
            db.update_role, project_id, role_id,
            payload.get("agent_id"), payload.get("profile"), payload.get("model"),
            payload.get("instructions"), payload.get("timeout_s"))
    except ValueError:
        raise HTTPException(status_code=404, detail="unknown role") from None
    if not data:
        raise HTTPException(status_code=404, detail="project not found")
    if agent_changed and agent_id is not None and not (find_agent(str(agent_id).strip()) or {}).get("model_selection"):
        await asyncio.to_thread(db.clear_role_selection, project_id, role_id)
    role = await asyncio.to_thread(db.get_role, project_id, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="role not found")
    selected_agent = find_agent(role.get("agent_id") or "") or {}
    resolved_profile, resolved_model = _validate_chat_profile(
        selected_agent, role.get("agent_id") or "", role.get("profile") or "", role.get("model"))
    role["supports_workdir"] = bool(_model_capabilities(
        selected_agent, role.get("agent_id") or "", resolved_profile, resolved_model
    ).get("supports_workdir"))
    return role


@app.get("/projects/{project_id}/conversations", dependencies=[Depends(require_token)])
async def conversations(project_id: str, limit: int = 0, cursor: str = "", q: str = "") -> dict:
    """Разговоры проекта: страницы по курсору и поиск по названию.

    Без limit отдаётся весь список — так работали прежние клиенты.
    """
    if not await asyncio.to_thread(db.get_project, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    if cursor and not db.decode_cursor(cursor):
        raise HTTPException(status_code=400, detail="bad cursor")
    page = await asyncio.to_thread(db.list_conversations_page, project_id,
                                   max(0, min(int(limit or 0), 200)), cursor, (q or "").strip()[:200])
    return page


@app.post("/projects/{project_id}/conversations", dependencies=[Depends(require_token)])
async def conversation_create(project_id: str, request: Request) -> dict:
    payload = await request.json()
    if not await asyncio.to_thread(db.get_project, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    role_id = (payload.get("role_id") or "").strip()
    if role_id and role_id not in db.ROLE_IDS:
        raise HTTPException(status_code=400, detail="unknown role")
    conv = await asyncio.to_thread(db.create_conversation, project_id, role_id,
                                   (payload.get("title") or "").strip()[:200])
    return conv


@app.get("/conversations/{conversation_id}", dependencies=[Depends(require_token)])
async def conversation_get(conversation_id: str) -> dict:
    conv = await asyncio.to_thread(db.get_conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    return conv


@app.get("/conversations/{conversation_id}/messages", dependencies=[Depends(require_token)])
async def conversation_messages(conversation_id: str, limit: int = 200) -> dict:
    if not await asyncio.to_thread(db.get_conversation, conversation_id):
        raise HTTPException(status_code=404, detail="conversation not found")
    items = await asyncio.to_thread(db.get_messages, conversation_id, max(1, min(limit, 500)))
    for message in items:
        message["attachments"] = [_public_attachment(a) for a in (message.get("attachments") or [])]
    return {"messages": items}


@app.get("/projects/{project_id}/attachments", dependencies=[Depends(require_token)])
async def attachment_list(project_id: str) -> dict:
    if not await asyncio.to_thread(db.get_project, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    items = await asyncio.to_thread(db.list_attachments, project_id)
    return {"attachments": [_public_attachment(item) for item in items]}


@app.post("/projects/{project_id}/attachments", dependencies=[Depends(require_token)])
async def attachment_upload(project_id: str, request: Request) -> dict:
    """Upload UTF-8 source/text or validated PNG/JPEG. Content is never executed/rendered."""
    payload = await request.json()
    name = (payload.get("filename") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="filename is required")
    low = name.lower()
    suffix = Path(low).suffix
    is_image = suffix in IMAGE_EXT
    if not is_image and suffix not in ALLOWED_EXT:
        raise HTTPException(status_code=415,
                            detail=f"format not supported: {name}; accepted: "
                                   + ", ".join((*ALLOWED_EXT, ".png", ".jpg", ".jpeg")))
    import base64 as _b64
    try:
        data = _b64.b64decode(payload.get("content_b64") or "", validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64") from None
    if not data:
        raise HTTPException(status_code=400, detail="file is empty")
    if len(data) > MAX_ATTACH_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"file too large: {len(data)} > {MAX_ATTACH_BYTES} bytes")
    if not await asyncio.to_thread(db.get_project, project_id):
        raise HTTPException(status_code=404, detail="project not found")
    mime = (payload.get("mime") or ("image/png" if suffix == ".png" else
                                   "image/jpeg" if is_image else "text/plain"))
    mime = mime.split(";", 1)[0].strip().lower()
    metadata = None
    if is_image:
        if mime not in IMAGE_MIME:
            raise HTTPException(status_code=415, detail="only PNG and JPEG images are supported")
        try:
            metadata = validate_image(data, mime, name)
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from None
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=415, detail="only UTF-8 text files are supported") from None
        if mime not in TEXT_MIME_OK:
            raise HTTPException(status_code=415, detail=f"unsupported text MIME type: {mime}")
    host_path = (payload.get("host_path") or "").strip() or None
    origin = "host-file" if host_path else "upload"
    att = await asyncio.to_thread(db.create_attachment, project_id, origin, name,
                                  mime, data, None, host_path, metadata)
    return _public_attachment(att)


@app.get("/attachments/{attachment_id}", dependencies=[Depends(require_token)])
async def attachment_get(attachment_id: str) -> dict:
    att = await asyncio.to_thread(db.get_attachment, attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="attachment not found")
    return _public_attachment(att)


def _public_attachment(att: dict) -> dict:
    item = dict(att)
    item.pop("storage", None)
    item["metadata"] = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    item["kind"] = "image" if (item.get("mime") or "").lower() in IMAGE_MIME else "text"
    if item["kind"] == "image":
        item["preview_url"] = f"/attachments/{item['id']}/preview"
    return item


@app.get("/attachments/{attachment_id}/preview", dependencies=[Depends(require_token)])
async def attachment_preview(attachment_id: str) -> Response:
    att = await asyncio.to_thread(db.get_attachment, attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="attachment not found")
    mime = (att.get("mime") or "").lower()
    if mime not in IMAGE_MIME:
        raise HTTPException(status_code=415, detail="preview is available only for PNG/JPEG images")
    import base64 as _b64
    try:
        data = _b64.b64decode(att.get("storage") or "", validate=True)
        validate_image(data, mime, att.get("filename") or "")
    except (ValueError, Exception):
        raise HTTPException(status_code=500, detail="stored image content is invalid") from None
    return Response(content=data, media_type=mime, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


def _resolve_chat_target(payload: dict, project: dict) -> tuple[dict, dict, str, str, str, str]:
    """Назначение чата: роль → агент (+переопределения из запроса). Только codex в 9.2."""
    role_id = (payload.get("role_id") or "").strip()
    role = None
    if role_id:
        if role_id not in db.ROLE_IDS:
            raise HTTPException(status_code=400, detail="unknown role")
        role = (db.get_role(project["id"], role_id) or {})
    agent_id = (payload.get("agent_id") or (role or {}).get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(status_code=400, detail="role has no agent assigned")
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    bridge_agent = item.get("bridge_agent")
    if not bridge_agent:
        raise HTTPException(status_code=501, detail=f"adapter for '{agent_id}' is not implemented yet")
    # A configured workdir must belong to the executor's declared host.
    host_id = (project.get("host_id") or "laptop").strip() or "laptop"
    agent_host = (item.get("host") or "").strip()
    if item.get("transport") != "http" and project.get("workdir") and agent_host != host_id:
        raise HTTPException(status_code=400,
                            detail=f"agent '{agent_id}' runs on '{agent_host or 'an undeclared host'}' but this project path "
                                   f"belongs to '{host_id}'; choose a matching agent or project host")
    profile_override = (payload.get("profile") or "").strip()
    model_override = (payload.get("model") or "").strip()
    if (profile_override or model_override) and not item.get("model_selection"):
        raise HTTPException(status_code=400,
                            detail=f"agent '{agent_id}' does not support profile/model selection")
    profile = (profile_override or ((role or {}).get("profile") or "")).strip() or None
    model = (model_override or ((role or {}).get("model") or "")).strip() or None
    if not item.get("model_selection"):
        profile, model = None, None
    raw_timeout = payload.get("timeout_s")
    if raw_timeout is None:
        raw_timeout = (role or {}).get("timeout_s")
    timeout_s = 600 if raw_timeout is None else raw_timeout
    try:
        timeout_s = int(timeout_s)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout must be an integer") from None
    if not (MIN_TIMEOUT_S <= timeout_s <= MAX_TIMEOUT_S):
        raise HTTPException(status_code=400, detail="timeout out of range")
    return item, role or {}, agent_id, profile or "", model or "", timeout_s


def _validate_chat_profile(item: dict, agent_id: str, profile: str, model: str | None) -> tuple[str, str | None]:
    """Валидация профиля/модели как в /run: дефолты из реестра, несовместимое — 400."""
    prof = AGENT_PROFILES.get(agent_id) or {}
    known_profiles = prof.get("profiles") or []
    default_profile = prof.get("default_profile") or ""
    if not profile:
        profile = default_profile
    spec = next((p for p in known_profiles if p.get("id") == profile), None)
    if profile and spec is None:
        raise HTTPException(status_code=400, detail=f"unknown provider profile '{profile}'")
    if spec is not None:
        known_models = spec.get("models") or []
        if model and model not in known_models:
            raise HTTPException(status_code=400,
                                detail=f"model '{model}' not supported by profile '{profile}'")
        if not model and known_models:
            model = known_models[0]
    return profile, model


def _model_capabilities(item: dict, agent_id: str, profile: str, model: str | None) -> dict:
    profile_spec = next((p for p in (AGENT_PROFILES.get(agent_id, {}).get("profiles") or [])
                         if p.get("id") == profile), {})
    model_spec = (profile_spec.get("model_capabilities") or {}).get(model) or {}
    defaults = {
        "input_modalities": ["text"], "output_modalities": ["text"],
        "required_modalities": [], "requires_image": False,
        "context_mode": "Режим задаётся адаптером агента",
        "filesystem_access": False if item.get("transport") == "http" else None,
        "supports_workdir": bool(item.get("supports_workdir")),
    }
    caps = defaults | {k: v for source in (item, profile_spec, model_spec)
                       for k, v in source.items() if k in defaults}
    caps["input_modalities"] = list(caps.get("input_modalities") or ["text"])
    caps["output_modalities"] = list(caps.get("output_modalities") or ["text"])
    caps["required_modalities"] = list(caps.get("required_modalities") or [])
    return caps


def _choose_required_image(attachment_refs: list[dict], image_refs: list[dict],
                           selected_ids: set[str]) -> tuple[list[dict], list[dict], dict, list[str]]:
    selected_images = [ref for ref in image_refs if ref["id"] in selected_ids]
    if len(selected_images) > 1:
        raise HTTPException(status_code=400, detail="Для OCR можно выбрать только одно изображение за запрос.")
    if selected_images:
        active = selected_images[0]
    elif image_refs:
        # image_refs are newest-first for history after the selected refs.
        active = image_refs[0]
    else:
        raise HTTPException(status_code=400,
                            detail="Для OCR загрузите PNG/JPEG или продолжите чат, где уже есть изображение.")
    active_id = active["id"]
    skipped = {ref["id"] for ref in image_refs if ref["id"] != active_id}
    refs = [ref for ref in attachment_refs if ref["id"] not in skipped]
    images = [ref for ref in image_refs if ref["id"] == active_id]
    warnings = []
    if active.get("source") == "conversation":
        warnings.append(f"Повторно используется последнее изображение из истории: {active['filename']}.")
    if skipped:
        warnings.append("Для OCR используется одно изображение; более старые изображения этого чата пропущены.")
    return refs, images, {
        "id": active["id"], "filename": active["filename"],
        "source": active["source"], "reused": active.get("source") == "conversation",
    }, warnings


def _omit_historical_images_for_text(attachment_refs: list[dict], image_refs: list[dict],
                                     selected_ids: set[str]) -> tuple[list[dict], list[dict], list[str]]:
    incompatible_selected = [ref for ref in image_refs if ref["id"] in selected_ids]
    if incompatible_selected:
        raise HTTPException(status_code=400, detail="selected agent does not accept image input")
    old_ids = {ref["id"] for ref in image_refs}
    if not old_ids:
        return attachment_refs, image_refs, []
    return ([ref for ref in attachment_refs if ref["id"] not in old_ids], [],
            ["Изображения из истории пропущены: выбранная модель принимает только текст."])


@app.post("/conversations/{conversation_id}/send", dependencies=[Depends(require_token)])
async def conversation_send(conversation_id: str, request: Request) -> JSONResponse:
    """Сообщение пользователя → запуск роли → ответ агента возвращается в чат."""
    payload = await request.json()
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > MAX_PROMPT_CHARS:
        raise HTTPException(status_code=413, detail="prompt too long")
    conv = await asyncio.to_thread(db.get_conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="conversation not found")
    project_id = (payload.get("project_id") or conv["project_id"]).strip()
    if project_id != conv["project_id"]:
        raise HTTPException(status_code=400, detail="conversation does not belong to project_id")
    project = await asyncio.to_thread(db.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")

    if not payload.get("role_id") and conv.get("role_id"):
        payload["role_id"] = conv["role_id"]
    item, role, agent_id, profile, model, timeout_s = _resolve_chat_target(payload, project)
    profile, model = _validate_chat_profile(item, agent_id, profile, model)
    capabilities = _model_capabilities(item, agent_id, profile, model)
    raw_attachments = payload.get("attachments") or []
    if not isinstance(raw_attachments, list):
        raise HTTPException(status_code=400, detail="attachments must be a list of attachment IDs")
    if len(raw_attachments) > 5:
        raise HTTPException(status_code=400, detail="at most 5 attachments may be sent per message")
    if any(not isinstance(a, str) or not a.strip() for a in raw_attachments):
        raise HTTPException(status_code=400, detail="each attachment must be a non-empty ID")
    attachment_ids = list(dict.fromkeys(a.strip() for a in raw_attachments))
    duplicate_attachment_ids = len(attachment_ids) != len(raw_attachments)

    # Look up accepted work before reading history: a retry's own user/agent messages
    # must not become part of a newly generated prompt.
    idem = payload.get("idempotency_key") or request.headers.get("idempotency-key")
    if idem is not None:
        if not isinstance(idem, str) or not idem.strip() or len(idem.strip()) > 200:
            raise HTTPException(status_code=400, detail="idempotency_key must be 1..200 characters")
        idem = idem.strip()
    if not idem:
        idem = "chat:" + hashlib.sha256(
            f"{conversation_id}|{text}|{agent_id}|{profile}|{model}|{','.join(attachment_ids)}".encode()).hexdigest()[:32]
    request_sha = hashlib.sha256(json.dumps({
        "conversation_id": conversation_id, "project_id": project_id, "text": text,
        "agent_id": agent_id, "profile": profile, "model": model,
        "role_id": payload.get("role_id") or "", "timeout_s": timeout_s,
        "attachments": attachment_ids,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    old_job = await asyncio.to_thread(db.get_job_by_idempotency_key, idem + ":j")
    if old_job:
        snapshot = old_job.get("run_snapshot") or {}
        if snapshot.get("chat_request_sha") != request_sha:
            raise HTTPException(status_code=409, detail="idempotency_key already used with another payload")
        conversation_title = await asyncio.to_thread(db.refresh_default_conversation_title, conversation_id)
        if conversation_title is None:
            conversation_title = conv.get("title") or ""
        old_messages = await asyncio.to_thread(db.get_messages, conversation_id, 500)
        old_agent = next((m for m in old_messages if m.get("job_id") == old_job["id"]), None)
        return JSONResponse(status_code=202, content={
            "message_id": old_agent["id"] if old_agent else None,
            "job_id": old_job["id"], "status": old_job["status"], "created": False,
            "agent": agent_id, "model": model, "profile": profile,
            "context_warnings": snapshot.get("context_warnings") or [],
            "conversation_title": conversation_title,
            "active_image": snapshot.get("active_image"),
            "active_image_id": (snapshot.get("active_image") or {}).get("id"),
            "active_image_filename": (snapshot.get("active_image") or {}).get("filename"),
            "active_image_source": (snapshot.get("active_image") or {}).get("source"),
            "active_image_reused": (snapshot.get("active_image") or {}).get("reused", False),
        })
    if (item.get("transport") != "http" and project.get("workdir")
            and not capabilities.get("supports_workdir")):
        raise HTTPException(status_code=400,
                            detail=f"agent '{agent_id}' cannot run in the project's workdir; "
                                   "choose a workdir-capable agent or clear the project workdir")
    try:
        quality.ensure_channel_enabled(agent_id, profile, model)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    prior_user_msg = await asyncio.to_thread(db.get_message_by_idempotency_key, idem + ":u")
    if prior_user_msg and (prior_user_msg.get("conversation_id") != conversation_id
                           or prior_user_msg.get("author") != "user"
                           or prior_user_msg.get("body") != text):
        raise HTTPException(status_code=409, detail="idempotency_key already used with another payload")

    # Only message-linked files from this conversation are eligible historical context.
    history = await asyncio.to_thread(db.get_conversation_context, conversation_id, 20)
    history_was_tail_limited = len(history) >= 20
    if prior_user_msg:
        history = [h for h in history if h.get("id") != prior_user_msg.get("id")]
    context_warnings = []
    role_instructions = (role.get("instructions") or "").strip()
    selected_rows: dict[str, dict] = {}
    for aid in attachment_ids:
        att = await asyncio.to_thread(db.get_attachment, aid)
        if not att or att["project_id"] != project_id:
            raise HTTPException(status_code=400, detail=f"unknown attachment '{aid}'")
        if att["origin"] == "host-file" and not att.get("storage"):
            raise HTTPException(status_code=400,
                                detail=f"файл '{att['filename']}' указан на устройстве агента, "
                                       "но содержимое не передано")
        selected_rows[aid] = att

    selected = [selected_rows[aid] for aid in attachment_ids]
    refs, ref_warnings = chat_context.collect_attachment_refs(
        selected, history[-12:], project_id, limit=5)
    context_warnings.extend(ref_warnings)
    rows_by_id: dict[str, dict] = {}
    valid_refs = []
    for ref in refs:
        att = selected_rows.get(ref["id"])
        if att is None:
            att = await asyncio.to_thread(db.get_attachment, ref["id"])
        if not att or att.get("project_id") != project_id:
            context_warnings.append(f"Вложение «{ref['filename']}» из истории недоступно и пропущено.")
            continue
        if att.get("origin") == "host-file" and not att.get("storage"):
            context_warnings.append(f"Файл «{att.get('filename','attachment')}» из истории недоступен и пропущен.")
            continue
        rows_by_id[ref["id"]] = att
        valid_refs.append(ref)
    attachment_refs = valid_refs
    text_attachments, image_refs, binary_refs, file_warnings = chat_context.prepare_attachment_payloads(
        attachment_refs, rows_by_id)
    context_warnings.extend(file_warnings)
    supported_inputs = set(capabilities["input_modalities"])
    selected_ids = set(attachment_ids)
    selected_binary = [ref for ref in binary_refs if ref["id"] in selected_ids]
    if selected_binary:
        raise HTTPException(status_code=400,
                            detail="selected binary attachments are not supported; upload UTF-8 text or PNG/JPEG")
    historical_binary_ids = {ref["id"] for ref in binary_refs if ref["id"] not in selected_ids}
    if historical_binary_ids:
        attachment_refs = [ref for ref in attachment_refs if ref["id"] not in historical_binary_ids]
        binary_refs = [ref for ref in binary_refs if ref["id"] not in historical_binary_ids]
        context_warnings.append(
            "Неподдерживаемые бинарные вложения из истории пропущены; доступны UTF-8 текст и PNG/JPEG.")
    if "image" not in supported_inputs:
        attachment_refs, image_refs, modality_warnings = _omit_historical_images_for_text(
            attachment_refs, image_refs, selected_ids)
        context_warnings.extend(modality_warnings)
    required = set(capabilities["required_modalities"])
    if capabilities.get("requires_image"):
        required.add("image")
    active_image_ref = None
    if "image" in required:
        attachment_refs, image_refs, active_image_ref, image_warnings = _choose_required_image(
            attachment_refs, image_refs, selected_ids)
        context_warnings.extend(image_warnings)
    if item.get("transport") == "http":
        context_warnings.append("Ответ по переданному контексту; модель не имеет доступа к файлам проекта или file tools.")
    try:
        full_prompt, prompt_warnings, context_message_ids = chat_context.build_chat_prompt(
            text, role.get("role_id", ""), role_instructions, history,
            text_attachments, image_refs, binary_refs, MAX_PROMPT_CHARS,
            history_was_tail_limited=history_was_tail_limited)
    except ValueError:
        raise HTTPException(status_code=413, detail="prompt with context too long") from None
    context_warnings.extend(prompt_warnings)
    if duplicate_attachment_ids:
        context_warnings.append("Повторно выбранное вложение учтено один раз.")
    context_warnings = list(dict.fromkeys(context_warnings))

    prompt_sha = hashlib.sha256(full_prompt.encode("utf-8")).hexdigest()
    # The chat key is persisted on both the user message and the queued job.

    # Сообщение пользователя — сразу, с ключом идемпотентности (повтор не дублирует).
    user_msg = await asyncio.to_thread(db.add_message, conversation_id, "user", text,
                                       None, "done", idem + ":u")
    conversation_title = await asyncio.to_thread(db.refresh_default_conversation_title, conversation_id)
    if conversation_title is None:
        conversation_title = conv.get("title") or ""
    for aid in attachment_ids:
        await asyncio.to_thread(db.link_attachment, user_msg["id"], aid)

    snapshot_attachment_refs = [
        {key: ref.get(key) for key in ("id", "version", "filename", "mime", "origin", "source", "message_id", "project_id")}
        for ref in attachment_refs
    ]
    snapshot_attachment_ids = [ref["id"] for ref in snapshot_attachment_refs]
    run_snapshot = {
        "project_id": project_id,
        "project_name": project.get("name"),
        "conversation_title": conversation_title,
        "host_id": item.get("host") if item.get("transport") == "http" else project.get("host_id"),
        "workdir": None if item.get("transport") == "http" else project.get("workdir"),
        "project_host_id": project.get("host_id"),
        "project_workdir": project.get("workdir"),
        "role_id": role.get("role_id") or (payload.get("role_id") or ""),
        "role_instructions": role_instructions,
        "agent_id": agent_id,
        "provider_profile": profile,
        "model": model,
        "input_modalities": capabilities["input_modalities"],
        "output_modalities": capabilities["output_modalities"],
        "required_modalities": capabilities["required_modalities"],
        "context_mode": capabilities["context_mode"],
        "filesystem_access": capabilities["filesystem_access"],
        "active_image": ({
            "id": active_image_ref["id"],
            "filename": active_image_ref["filename"],
            "source": active_image_ref["source"],
            "reused": active_image_ref["source"] == "conversation",
        } if active_image_ref else None),
        "selected_attachment_ids": attachment_ids,
        "attachment_ids": snapshot_attachment_ids,
        "attachment_versions": {ref["id"]: ref["version"] for ref in snapshot_attachment_refs},
        "attachment_refs": snapshot_attachment_refs,
        "context_message_ids": context_message_ids,
        "chat_request_sha": request_sha,
        "context_warnings": context_warnings,
        "task_text": text,
        "supports_workdir": capabilities.get("supports_workdir", False),
    }
    try:
        job, created = await asyncio.to_thread(
            db.create_chat_job, item.get("bridge_agent") or agent_id, full_prompt, prompt_sha,
            timeout_s, idem + ":j", MAX_QUEUE, model, profile,
            project_id, conversation_id, run_snapshot["role_id"], run_snapshot)
    except ValueError:
        raise HTTPException(status_code=409, detail="idempotency_key already used") from None
    except db.QueueFull as exc:
        raise HTTPException(status_code=429, detail=f"queue is full ({exc})") from None

    # Сообщение-«ожидание» ответа: результат допишется сюда же, а не новым сообщением.
    pending_state = "queued" if job.get("status") == "queued" else "running"
    wait_msg = await asyncio.to_thread(db.add_message, conversation_id, "agent",
                                       "В очереди…" if pending_state == "queued" else "Выполняю…",
                                       job["id"], pending_state, idem + ":w")
    # A very fast worker can finish before the placeholder is inserted. Catch that
    # up here; after this read, any later callback sees the message and updates it.
    latest_job = await asyncio.to_thread(db.get_job, job["id"])
    if latest_job and latest_job.get("status") in ("succeeded", "failed", "timeout", "cancelled"):
        result = latest_job.get("result_output") or latest_job.get("result_error") or "Задание завершено без текста ответа."
        state = "done" if latest_job.get("status") == "succeeded" else "error"
        await asyncio.to_thread(db.finish_message_by_job, job["id"], result, state)
    return JSONResponse(status_code=202, content={
        "message_id": wait_msg["id"], "job_id": job["id"], "status": job["status"],
        "created": created, "agent": agent_id, "model": model, "profile": profile,
        "context_warnings": context_warnings,
        "conversation_title": conversation_title,
        "active_image": run_snapshot.get("active_image"),
        "active_image_id": (run_snapshot.get("active_image") or {}).get("id"),
        "active_image_filename": (run_snapshot.get("active_image") or {}).get("filename"),
        "active_image_source": (run_snapshot.get("active_image") or {}).get("source"),
        "active_image_reused": (run_snapshot.get("active_image") or {}).get("reused", False),
    })


@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
async def cancel(job_id: str) -> dict:
    status = await asyncio.to_thread(db.cancel_job, job_id)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="job not found")
    await asyncio.to_thread(quality.refresh_job, job_id)
    return {"job_id": job_id, "status": status}


@app.post("/bridge/worker/heartbeat", dependencies=[Depends(require_token)])
async def worker_heartbeat(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="heartbeat must be a JSON object") from None
    try:
        cards = load_agents().get("agents", [])
        return await asyncio.to_thread(workers.heartbeat, body, None, cards)
    except workers.ProtocolError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@app.get("/resources", dependencies=[Depends(require_token)])
async def resources_list() -> dict:
    items = await asyncio.to_thread(resources.list_leases)
    return {"count": len(items), "resources": items}


@app.post("/resources/release", dependencies=[Depends(require_token)])
async def resources_release(request: Request) -> dict:
    """Освободить аренду ресурса по ключу и владельцу-заданию.

    Только явное действие оператора: аренда чужого задания так не снимается,
    потому что ключ и job_id должны совпасть.
    """
    payload = await request.json()
    key = str(payload.get("key") or "").strip()
    job_id = str(payload.get("job_id") or "").strip()
    if not key or not job_id:
        raise HTTPException(status_code=400, detail="key and job_id are required")
    released = await asyncio.to_thread(resources.release, key, job_id)
    return {"ok": released, "key": key, "job_id": job_id}


@app.get("/workspace/ops", dependencies=[Depends(require_token)])
async def workspace_ops() -> dict:
    """Каталог разрешённых операций: что можно попросить у устройства-воркера."""
    return {
        "ops": [{"name": name, "description": text} for name, text in workspace.OPS.items()],
        "mutating": sorted(workspace.WRITES_ROOT),
        "limits": {"list_files": workspace.MAX_LIST, "read_files": workspace.MAX_READ_FILES,
                   "read_bytes": workspace.MAX_READ_BYTES,
                   "patch_files": workspace.MAX_PATCH_FILES,
                   "patch_bytes": workspace.MAX_PATCH_BYTES,
                   "checks": workspace.MAX_CHECKS},
    }


@app.post("/workspace/jobs", dependencies=[Depends(require_token)])
async def workspace_job_create(request: Request) -> JSONResponse:
    """Ставит control-задание на устройство. Модель не присылает команды — только op+args."""
    payload = await request.json()
    try:
        result = await asyncio.to_thread(
            workspace.create_op_job,
            payload.get("op") or "",
            payload.get("args") or {},
            payload.get("host_id") or "",
            payload.get("project_id"),
            payload.get("idempotency_key"),
            payload.get("timeout_s"),
        )
    except workspace.OpError as exc:
        status = 409 if exc.code == "idempotency_conflict" else 400
        return JSONResponse(status_code=status, content={"error": exc.as_dict()})
    job = result["job"]
    return JSONResponse({
        "job_id": job["id"],
        "status": job.get("status"),
        "kind": job.get("kind"),
        "op": result["op"],
        "host_id": result["host_id"],
        "root": result["root"],
        "digest": result["digest"],
        "idempotency_key": result["idempotency_key"],
        "reused": not result["created"],
        "receipt": result["receipt"],
    })


@app.get("/workspace/jobs/{job_id}", dependencies=[Depends(require_token)])
async def workspace_job_get(job_id: str) -> dict:
    """Состояние и результат операции: результат уже разобран и лежит в receipt."""
    result = await asyncio.to_thread(workspace.job_result, job_id)
    if not result:
        raise HTTPException(status_code=404, detail="unknown job")
    return result


@app.post("/projects/{project_id}/context/packets", dependencies=[Depends(require_token)])
async def context_packet_create(project_id: str, request: Request) -> JSONResponse:
    """Собирает пакет входа: перечень файлов фиксируется хостом, не хабом."""
    project = await asyncio.to_thread(db.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    payload = await request.json()
    paths = payload.get("paths") or []
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise HTTPException(status_code=400, detail="paths must be a list of strings")
    budget = payload.get("budget_bytes")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise HTTPException(status_code=400, detail="budget_bytes must be a positive integer")
    try:
        result = await asyncio.to_thread(
            packets.start, project, paths, budget, payload.get("conversation_id"))
    except packets.PacketError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None
    return JSONResponse(status_code=202, content=result)


@app.get("/projects/{project_id}/context/packets", dependencies=[Depends(require_token)])
async def context_packets(project_id: str, limit: int = 50) -> dict:
    items = await asyncio.to_thread(packets.list_for_project, project_id, max(1, min(limit, 200)))
    return {"count": len(items), "items": items}


@app.get("/context/packets/{packet_id}", dependencies=[Depends(require_token)])
async def context_packet(packet_id: str) -> dict:
    item = await asyncio.to_thread(packets.get, packet_id)
    if not item:
        raise HTTPException(status_code=404, detail="packet not found")
    return item


@app.post("/context/packets/{packet_id}/recheck", dependencies=[Depends(require_token)])
async def context_packet_recheck(packet_id: str) -> dict:
    try:
        return await asyncio.to_thread(packets.start_recheck, packet_id)
    except packets.PacketError as exc:
        status = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status, detail=exc.as_dict()) from None
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None


@app.post("/context/packets/{packet_id}/materialize", dependencies=[Depends(require_token)])
async def context_packet_materialize(packet_id: str) -> dict:
    try:
        return await asyncio.to_thread(packets.start_materialize, packet_id)
    except packets.PacketError as exc:
        status = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status, detail=exc.as_dict()) from None
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None


def _resolve_run_agent(agent_id: str) -> tuple[str | None, str | None]:
    """Для задачи запуска: id агента -> bridge_agent, либо причина отказа."""
    item = find_agent(agent_id)
    if not item:
        return None, f"агент '{agent_id}' не найден в реестре"
    bridge = item.get("bridge_agent")
    if not bridge:
        return None, f"у агента '{agent_id}' нет адаптера"
    return bridge, None


@app.get("/templates/five-roles", dependencies=[Depends(require_token)])
async def five_roles_template(entry: str = "", contracts: str = "") -> dict:
    parsed = [item.strip() for item in (contracts or "").split(",") if item.strip()]
    return templates_ctrl.describe() | {"template": templates_ctrl.template(entry, parsed)}


@app.post("/runs", dependencies=[Depends(require_token)])
async def run_create(request: Request) -> JSONResponse:
    payload = await request.json()
    if payload.get("template_id") == templates_ctrl.FIVE_ROLES_ID:
        payload["template"] = templates_ctrl.template(
            payload.get("entry") or "",
            [c for c in (payload.get("contracts") or []) if isinstance(c, str)],
            agent=(payload.get("agent") or "codex"))
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id is required")
    goal = payload.get("goal")
    if goal is not None and not isinstance(goal, str):
        raise HTTPException(status_code=400, detail="goal must be a string")
    criteria = payload.get("criteria") or []
    if not isinstance(criteria, list) or any(not isinstance(c, str) for c in criteria):
        raise HTTPException(status_code=400, detail="criteria must be a list of strings")
    request_key = payload.get("request_key")
    if request_key is not None and (not isinstance(request_key, str) or not request_key.strip()):
        raise HTTPException(status_code=400, detail="request_key must be a non-empty string")
    project = await asyncio.to_thread(db.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    restore = await asyncio.to_thread(_ensure_restore_point, project,
                                      str(request_key or "run")[:48] or "run")
    if not restore.get("ok"):
        raise HTTPException(status_code=409, detail={"code": "backup_unconfirmed",
                                                     "message": restore.get("error"),
                                                     "job": restore.get("job")})
    try:
        result = await asyncio.to_thread(
            runs_ctrl.create_run, project_id, payload.get("template") or {},
            payload.get("policy") or {}, goal=goal or "",
            criteria=criteria, request_key=(request_key or "").strip() or None,
            require_ready_host=True)
    except runs_ctrl.RunError as exc:
        if exc.code == "not_found":
            raise HTTPException(status_code=404, detail=exc.as_dict()) from None
        if exc.code in {"idempotency_conflict", "runner_unavailable"}:
            raise HTTPException(status_code=409, detail=exc.as_dict()) from None
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None
    if not result.get("existing"):
        # Отдельная рабочая копия задачи от зафиксированного SHA: кодер и все
        # проверки работают в ней, основная ветка до приёмки не меняется.
        try:
            copy = await asyncio.to_thread(runs_ctrl.start_task_copy, project_id,
                                           result["run_id"])
        except runs_ctrl.RunError as exc:
            raise HTTPException(status_code=409, detail={
                "code": exc.code, "message": str(exc), "run_id": result["run_id"]}) from None
        result["task_root"] = copy.get("task_root")
        result["task_branch"] = copy.get("branch")
        result["base_sha"] = copy.get("base_sha")
        settings = project.get("settings") if isinstance(project.get("settings"), dict) else {}
        setup = settings.get("env_setup") if isinstance(settings.get("env_setup"), dict) else {}
        if setup.get("cmd"):
            prepared = await asyncio.to_thread(runs_ctrl.setup_env, result["run_id"], setup)
            if not prepared.get("ok"):
                raise HTTPException(status_code=409, detail={
                    "code": "setup_failed", "message": prepared.get("error"),
                    "run_id": result["run_id"]})
            result["env_setup"] = "ok"
    # Идентичный повтор с тем же ключом возвращает прежний запуск (200), новый
    # запрос — 202. Расхождение содержимого под тем же ключом — 409 выше.
    status = 200 if result.get("existing") else 202
    return JSONResponse(status_code=status, content=result)


@app.get("/runs", dependencies=[Depends(require_token)])
async def runs_list(project_id: str | None = None, limit: int = 30) -> dict:
    items = await asyncio.to_thread(runs_ctrl.list_runs, project_id, max(1, min(limit, 100)))
    return {"count": len(items), "items": items}


@app.get("/runs/{run_id}", dependencies=[Depends(require_token)])
async def run_get(run_id: str) -> dict:
    item = await asyncio.to_thread(runs_ctrl.get_run, run_id)
    if not item:
        raise HTTPException(status_code=404, detail="run not found")
    return item


@app.post("/runs/{run_id}/dispatch", dependencies=[Depends(require_token)])
async def run_dispatch(run_id: str) -> dict:
    try:
        return await asyncio.to_thread(runs_ctrl.dispatch, run_id, _resolve_run_agent)
    except runs_ctrl.RunError as exc:
        raise HTTPException(status_code=404, detail=exc.as_dict()) from None


@app.post("/runs/{run_id}/{action}", dependencies=[Depends(require_token)])
async def run_action(run_id: str, action: str) -> dict:
    if action not in ("pause", "resume", "cancel"):
        raise HTTPException(status_code=400, detail="action must be pause, resume or cancel")
    try:
        return await asyncio.to_thread(runs_ctrl.set_state, run_id, action)
    except runs_ctrl.RunError as exc:
        code = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=code, detail=exc.as_dict()) from None


@app.post("/projects/{project_id}/context/requests", dependencies=[Depends(require_token)])
async def context_request_create(project_id: str, request: Request) -> JSONResponse:
    """Собирает вход под конкретную задачу: точка входа, импорты, обязательные контракты."""
    project = await asyncio.to_thread(db.get_project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    payload = await request.json()
    budget = payload.get("budget_tokens")
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise HTTPException(status_code=400, detail="budget_tokens must be a positive integer")
    contracts = payload.get("contracts") or []
    if not isinstance(contracts, list) or any(not isinstance(c, str) for c in contracts):
        raise HTTPException(status_code=400, detail="contracts must be a list of strings")
    try:
        result = await asyncio.to_thread(
            packets.create_context, project, payload.get("goal") or "",
            payload.get("entry") or "", contracts, budget, payload.get("conversation_id"))
    except packets.PacketError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None
    return JSONResponse(status_code=202, content=result)


@app.get("/context/requests/{request_id}", dependencies=[Depends(require_token)])
async def context_request_get(request_id: str) -> dict:
    item = await asyncio.to_thread(packets.get, request_id)
    if not item:
        raise HTTPException(status_code=404, detail="context request not found")
    manifest = item.get("manifest") or {}
    return {
        "id": item["id"], "project_id": item["project_id"], "state": item["state"],
        "revision": item["revision"], "digest": item.get("digest"),
        "created_at": item.get("created_at"), "sealed_at": item.get("sealed_at"),
        "entry": (item.get("request") or {}).get("entry"),
        "contracts": (item.get("request") or {}).get("contracts") or [],
        "goal": (item.get("request") or {}).get("goal"),
        "budget_tokens": manifest.get("budget_tokens"),
        "used_tokens": manifest.get("used_tokens"),
        "total_project_tokens": manifest.get("total_project_tokens"),
        "saved_tokens": manifest.get("saved_tokens"),
        "saved_ratio": manifest.get("saved_ratio"),
        "imports": manifest.get("imports") or [],
        "error": manifest.get("error"),
        "items": item.get("items") or [],
    }


@app.post("/context/requests/{request_id}/recheck", dependencies=[Depends(require_token)])
async def context_request_recheck(request_id: str) -> dict:
    try:
        return await asyncio.to_thread(packets.start_recheck, request_id)
    except packets.PacketError as exc:
        status = 404 if exc.code == "not_found" else 409
        raise HTTPException(status_code=status, detail=exc.as_dict()) from None
    except workspace.OpError as exc:
        raise HTTPException(status_code=400, detail=exc.as_dict()) from None


@app.get("/workspace/reconciliation/cases", dependencies=[Depends(require_token)])
async def workspace_reconciliation_cases(
        cursor: str | None = None, limit: int = 50,
        decision_state: str = "unresolved", job_status: str = "all") -> dict:
    try:
        return await asyncio.to_thread(reconciliation.list_cases, cursor, limit,
                                       decision_state, job_status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/workspace/reconciliation/cases/{case_id}", dependencies=[Depends(require_token)])
async def workspace_reconciliation_case(case_id: str) -> dict:
    result = await asyncio.to_thread(reconciliation.get_case, case_id)
    if not result:
        raise HTTPException(status_code=404, detail="unknown reconciliation case")
    return result


@app.post("/workspace/reconciliation/cases/{case_id}/decision",
          dependencies=[Depends(require_token)])
async def workspace_reconciliation_decide(case_id: str, request: Request) -> dict:
    if not HUB_TOKEN:
        raise HTTPException(status_code=503, detail="reconciliation requires configured Hub authentication")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    try:
        # The API has a shared bearer token, so the auditable principal is the
        # token holder; a client-supplied actor field is deliberately ignored.
        return await asyncio.to_thread(
            reconciliation.decide, case_id, payload.get("action"),
            payload.get("expected_revision"), payload.get("reason"), "hub-token-holder")
    except reconciliation.CaseNotFound:
        raise HTTPException(status_code=404, detail="unknown reconciliation case") from None
    except reconciliation.DecisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@app.get("/workers", dependencies=[Depends(require_token)])
async def workers_list() -> dict:
    items = await asyncio.to_thread(workers.list_workers)
    return {"count": len(items), "workers": items}


@app.get("/bridge/jobs", dependencies=[Depends(require_token)])
async def bridge_jobs(worker: str = "", agents: str = "",
                      protocol_ver: str | None = Query(None, alias="protocol"),
                      kinds: str = "agent") -> JSONResponse:
    # Версия протокола обязательна. Старый poller (v1) не арендует задания v2:
    # иначе он получал бы задание, не умея ни durable-ack, ни heartbeat, и
    # «терял» бы его при первом падении. Отказ явный — 426, а не пустая очередь.
    problem = bridge_protocol.client_problem(protocol_ver)
    if problem:
        return JSONResponse(status_code=426, content={
            "detail": problem,
            "supported": list(bridge_protocol.SUPPORTED),
            "current": bridge_protocol.PROTOCOL,
            "heartbeat_interval_s": bridge_protocol.heartbeat_interval(LEASE_SECONDS),
            "job": None,
        })
    client_protocol = int(bridge_protocol.version_of(protocol_ver))
    leased = await asyncio.to_thread(db.lease_job, worker or "unknown", LEASE_SECONDS, agents,
                                     client_protocol, kinds)
    if not leased:
        return JSONResponse({"job": None, "protocol": client_protocol,
                             "heartbeat_interval_s": bridge_protocol.heartbeat_interval(LEASE_SECONDS)})
    return JSONResponse({
        "id": leased["id"],
        "attempt_id": leased["attempt_id"],
        "kind": leased.get("kind") or "agent",
        "agent": leased["agent_id"],
        "prompt": leased["prompt"],
        "timeout": leased["timeout_s"],
        "lease_seconds": LEASE_SECONDS,
        "protocol": leased.get("protocol") or client_protocol,
        "resource": leased.get("resource"),
        "heartbeat_interval_s": bridge_protocol.heartbeat_interval(LEASE_SECONDS),
        "requested_model": leased.get("requested_model"),
        "requested_profile": leased.get("requested_profile"),
        "workdir": leased.get("workdir"),
        "host_id": leased.get("host_id"),
        "attachments": leased.get("attachments") or [],
        "project_host_id": (leased.get("run_snapshot") or {}).get("project_host_id"),
        "project_workdir": (leased.get("run_snapshot") or {}).get("project_workdir"),
        "input_modalities": (leased.get("run_snapshot") or {}).get("input_modalities") or ["text"],
        "context_mode": (leased.get("run_snapshot") or {}).get("context_mode"),
        "filesystem_access": (leased.get("run_snapshot") or {}).get("filesystem_access"),
        "task_text": (leased.get("run_snapshot") or {}).get("task_text"),
    })


@app.post("/bridge/heartbeat", dependencies=[Depends(require_token)])
async def bridge_heartbeat(request: Request) -> dict:
    payload = await request.json()
    attempt_id = payload.get("attempt_id")
    if not attempt_id:
        raise HTTPException(status_code=400, detail="attempt_id is required")
    detail = await asyncio.to_thread(db.heartbeat_detail, attempt_id, LEASE_SECONDS,
                                     payload.get("resource_generation"))
    if detail is None:
        return {"ok": False, "lease_seconds": LEASE_SECONDS}
    if detail["attempt_protocol"] != detail["job_protocol"]:
        raise HTTPException(status_code=409, detail=(
            "attempt protocol " + str(detail["attempt_protocol"])
            + " does not match job protocol " + str(detail["job_protocol"])))
    interval = bridge_protocol.heartbeat_interval(LEASE_SECONDS)
    # Воркер сообщает свой интервал: если он не короче аренды, это ошибка
    # настройки, и хаб говорит об этом прямо, а не молча продлевает аренду.
    reported = payload.get("hb_interval_s")
    risk = bridge_protocol.lease_risk(reported, LEASE_SECONDS) if reported is not None else None
    return {"ok": True, "lease_seconds": LEASE_SECONDS,
            "heartbeat_interval_s": interval, "lease_risk": risk,
            "protocol": detail["job_protocol"],
            "resource": detail.get("resource")}


@app.post("/bridge/result", dependencies=[Depends(require_token)])
async def bridge_result(request: Request) -> dict:
    payload = await request.json()
    attempt_id = payload.get("attempt_id")
    if not attempt_id:
        raise HTTPException(status_code=400, detail="attempt_id is required")
    output = payload.get("output")
    if isinstance(output, str) and len(output) > MAX_RESULT_CHARS:
        output = output[:MAX_RESULT_CHARS] + f"\n… (усечено хабом: {len(output)} символов)"
    outcome = await asyncio.to_thread(
        db.finish_attempt,
        attempt_id,
        bool(payload.get("ok")),
        output,
        payload.get("error"),
        payload.get("usage") or {},
        payload.get("elapsed"),
        payload.get("job_id"),
        bool(payload.get("uncertain")),
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="unknown attempt")
    if outcome == "attempt_mismatch":
        raise HTTPException(status_code=409, detail="job_id does not match attempt")
    if payload.get("job_id"):
        await asyncio.to_thread(quality.refresh_job, payload["job_id"])
    # Задание из чата: ответ агента дописывается в сообщение-«ожидание».
    if outcome == "applied" and payload.get("job_id"):
        try:
            state = "done" if payload.get("ok") else "failed"
            body = output if payload.get("ok") else (payload.get("error") or output or "Ошибка исполнения")
            await asyncio.to_thread(db.finish_message_by_job, payload["job_id"],
                                    body or "", state)
        except Exception as exc:
            print("chat finish failed:", exc)
    # CORE-02b: результат управляемой операции фиксируется как durable receipt —
    # поздний повтор с тем же ключом вернёт его, а не применит патч заново.
    if outcome in ("applied", "unknown", "late", "duplicate") and payload.get("job_id"):
        try:
            job_row = await asyncio.to_thread(db.get_job, payload["job_id"])
            if (job_row or {}).get("kind") == "workspace":
                envelope = json.loads(job_row.get("prompt") or "{}")
                body = output or ""
                try:
                    parsed = json.loads(body)
                except ValueError:
                    parsed = {"raw": body[:2000]}
                snap = job_row.get("run_snapshot") or {}
                key = job_row.get("idempotency_key") or ""
                existing_receipt = await asyncio.to_thread(db.workspace_receipt, key)
                # A late artifact may follow a previously recorded uncertain
                # receipt for this attempt. Keep the first immutable receipt;
                # the late payload remains separately stored by finish_attempt.
                record_late = (outcome != "late" or not existing_receipt or
                               (existing_receipt.get("status") == "pending"
                                and not existing_receipt.get("attempt_id")))
                if record_late:
                    if outcome == "late":
                        receipt_status = "late"
                    elif outcome == "unknown" or job_row.get("status") == "unknown":
                        receipt_status = "unknown"
                    elif job_row.get("status") == "succeeded":
                        receipt_status = "applied"
                    else:
                        receipt_status = "failed"
                    await asyncio.to_thread(
                        db.record_workspace_receipt, key, envelope.get("op") or "",
                        snap.get("host_id") or "", payload["job_id"],
                        envelope.get("digest") or "", receipt_status, parsed,
                        hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None,
                        attempt_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            print("workspace receipt failed:", type(exc).__name__, flush=True)
            raise HTTPException(status_code=503, detail="workspace receipt was not durably recorded") from None

    # late/stale: результат сохранён в late_results — воркер может удалять outbox,
    # но задание не переоткрывается и статус не переписывается.
    return {"ok": outcome in ("applied", "duplicate", "late", "stale", "unknown"),
            "outcome": outcome, "recorded": outcome != "not_found"}


def _llm_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if MASTER_KEY:
        headers["Authorization"] = f"Bearer {MASTER_KEY}"
    return headers


@app.get("/models", dependencies=[Depends(require_token)])
async def models() -> JSONResponse:
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(f"{LITELLM_URL}/v1/models", headers=_llm_headers())
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"litellm unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.post("/v1/chat/completions", dependencies=[Depends(require_token)])
async def chat(request: Request) -> Response:
    body = await request.body()
    async with httpx.AsyncClient(timeout=600) as client:
        try:
            resp = await client.post(
                f"{LITELLM_URL}/v1/chat/completions",
                content=body,
                headers=_llm_headers(),
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"litellm unreachable: {exc}") from exc
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


@app.middleware("http")
async def no_cache_ui(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/ui"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


if WEB_DIR.exists():
    app.mount("/ui", StaticFiles(directory=str(WEB_DIR), html=True), name="ui")


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")
