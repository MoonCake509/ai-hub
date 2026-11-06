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
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import db

VERSION = "0.3.0"
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


async def _sweeper() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            expired = await asyncio.to_thread(db.expire_leases)
            if expired:
                print(f"expired leases: {expired}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"lease sweeper error: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.init)
    load_profiles()
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
    return {"status": "ok", "service": "hub-agent", "version": VERSION, "db": db_ok}


@app.get("/agents")
def agents() -> dict:
    items = load_agents().get("agents", [])
    return {"count": len(items), "agents": items}


# Профили провайдеров и модели для выбора в интерфейсе. Источник — реестр агентов,
# а не внешние платные вызовы: хаб не расходует бюджет на перечисление каталога.
AGENT_PROFILES: dict[str, dict] = {}


def load_profiles() -> None:
    for item in load_agents().get("agents", []):
        AGENT_PROFILES[item["id"]] = {
            "agent_id": item["id"],
            "profiles": item.get("profiles") or [],
            "default_profile": item.get("default_profile") or "",
        }


@app.get("/agent-profiles/{agent_id}", dependencies=[Depends(require_token)])
async def agent_profiles(agent_id: str) -> dict:
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    prof = AGENT_PROFILES.get(agent_id) or {"profiles": [], "default_profile": ""}
    return {"agent_id": agent_id, **prof}


@app.get("/agents/{agent_id}")
def agent(agent_id: str) -> dict:
    item = find_agent(agent_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")
    return item


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

    try:
        job, created = await asyncio.to_thread(
            db.create_job, bridge_agent, prompt, prompt_sha, timeout_s, idem or None, MAX_QUEUE,
            requested_model)
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


@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
async def cancel(job_id: str) -> dict:
    status = await asyncio.to_thread(db.cancel_job, job_id)
    if status == "not_found":
        raise HTTPException(status_code=404, detail="job not found")
    return {"job_id": job_id, "status": status}


@app.get("/bridge/jobs", dependencies=[Depends(require_token)])
async def bridge_jobs(worker: str = "", agents: str = "") -> JSONResponse:
    leased = await asyncio.to_thread(db.lease_job, worker or "unknown", LEASE_SECONDS, agents)
    if not leased:
        return JSONResponse({"job": None})
    return JSONResponse({
        "id": leased["id"],
        "attempt_id": leased["attempt_id"],
        "agent": leased["agent_id"],
        "prompt": leased["prompt"],
        "timeout": leased["timeout_s"],
        "lease_seconds": LEASE_SECONDS,
        "requested_model": leased.get("requested_model"),
    })


@app.post("/bridge/heartbeat", dependencies=[Depends(require_token)])
async def bridge_heartbeat(request: Request) -> dict:
    payload = await request.json()
    attempt_id = payload.get("attempt_id")
    if not attempt_id:
        raise HTTPException(status_code=400, detail="attempt_id is required")
    alive = await asyncio.to_thread(db.heartbeat, attempt_id, LEASE_SECONDS)
    return {"ok": alive}


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
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="unknown attempt")
    # late/stale: результат сохранён в late_results — воркер может удалять outbox,
    # но задание не переоткрывается и статус не переписывается.
    return {"ok": outcome in ("applied", "duplicate", "late", "stale"),
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
