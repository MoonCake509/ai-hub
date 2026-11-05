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
from fastapi.responses import JSONResponse, Response

from . import db

VERSION = "0.3.0"
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
AGENTS_FILE = Path(os.environ.get("AGENTS_FILE", "/app/config/agents.yaml"))
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "300"))


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
    timeout_s = int(payload.get("timeout") or 600)
    idem = payload.get("idempotency_key") or request.headers.get("idempotency-key")
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    try:
        job, created = await asyncio.to_thread(
            db.create_job, bridge_agent, prompt, prompt_sha, timeout_s, idem or None)
    except ValueError:
        raise HTTPException(status_code=409, detail="idempotency_key already used with another payload") from None

    return JSONResponse(status_code=202, content={
        "job_id": job["id"],
        "status": job["status"],
        "created": created,
        "agent": agent_id,
    })


@app.get("/jobs", dependencies=[Depends(require_token)])
async def jobs(status: str | None = None, limit: int = 50) -> dict:
    items = await asyncio.to_thread(db.list_jobs, status, min(limit, 200))
    return {"count": len(items), "jobs": items}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
async def job(job_id: str) -> dict:
    data = await asyncio.to_thread(db.get_job, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="job not found")
    return data


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
    outcome = await asyncio.to_thread(
        db.finish_attempt,
        attempt_id,
        bool(payload.get("ok")),
        payload.get("output"),
        payload.get("error"),
        payload.get("usage") or {},
        payload.get("elapsed"),
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="unknown attempt")
    return {"ok": outcome in ("applied", "duplicate"), "outcome": outcome}


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