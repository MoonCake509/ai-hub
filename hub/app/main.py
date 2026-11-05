"""AI-Hub core agent.

Возможности:
  * реестр агентов (/agents) из config/agents.yaml;
  * прокси к LiteLLM (/models, /v1/chat/completions);
  * запуск агентов на удалённых хостах через pull-мост (/agents/{id}/run →
    /bridge/jobs + /bridge/result). Pull выбран потому, что ноутбук с Codex
    не принимает входящие соединения: брандмауэр блокирует вход, прав админа нет.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path

import httpx
import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

VERSION = "0.2.0"
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
HUB_TOKEN = os.environ.get("HUB_TOKEN", "")
AGENTS_FILE = Path(os.environ.get("AGENTS_FILE", "/app/config/agents.yaml"))

app = FastAPI(title="AI-Hub", version=VERSION)

JOBS: dict[str, dict] = {}
JOB_EVENTS: dict[str, asyncio.Event] = {}


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
    """Если HUB_TOKEN задан, вызовы обязаны предъявить его."""
    if not HUB_TOKEN:
        return
    if request.headers.get("authorization", "") != f"Bearer {HUB_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing token")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "hub-agent",
        "version": VERSION,
        "queue": sum(1 for j in JOBS.values() if j["status"] == "queued"),
        "running": sum(1 for j in JOBS.values() if j["status"] == "running"),
    }


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
    timeout = int(payload.get("timeout") or 600)

    job_id = uuid.uuid4().hex
    JOBS[job_id] = {
        "agent": bridge_agent,
        "prompt": prompt,
        "timeout": timeout,
        "status": "queued",
        "created": time.time(),
    }
    event = asyncio.Event()
    JOB_EVENTS[job_id] = event
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout + 30)
    except asyncio.TimeoutError:
        JOBS[job_id]["status"] = "timeout"
        raise HTTPException(status_code=504, detail="agent did not answer in time") from None
    finally:
        JOB_EVENTS.pop(job_id, None)

    result = JOBS.pop(job_id, {}).get("result", {})
    result.setdefault("agent", agent_id)
    return JSONResponse(status_code=200, content=result)


@app.get("/bridge/jobs", dependencies=[Depends(require_token)])
def bridge_jobs(worker: str = "") -> JSONResponse:
    """Выдаёт одно задание воркеру (pull) или пустой ответ."""
    for job_id, job in JOBS.items():
        if job["status"] == "queued":
            job["status"] = "running"
            job["worker"] = worker
            job["started"] = time.time()
            return JSONResponse({
                "id": job_id,
                "agent": job["agent"],
                "prompt": job["prompt"],
                "timeout": job["timeout"],
            })
    return JSONResponse({"job": None})


@app.post("/bridge/result", dependencies=[Depends(require_token)])
async def bridge_result(request: Request) -> dict:
    payload = await request.json()
    job_id = payload.get("job_id")
    job = JOBS.get(job_id or "")
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    job["result"] = payload
    job["status"] = "done"
    job["finished"] = time.time()
    event = JOB_EVENTS.get(job_id)
    if event is not None:
        event.set()
    return {"ok": True}


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
