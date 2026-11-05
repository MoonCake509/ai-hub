"""AI-Hub core agent.

MVP: реестр агентов + прокси к LiteLLM. Никакой бизнес-логики сверх этого,
пока не подключены адаптеры агентов.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

VERSION = "0.1.0"
LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000").rstrip("/")
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
AGENTS_FILE = Path(os.environ.get("AGENTS_FILE", "/app/config/agents.yaml"))

app = FastAPI(title="AI-Hub", version=VERSION)


def load_agents() -> dict:
    if not AGENTS_FILE.exists():
        return {"agents": []}
    with AGENTS_FILE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {"agents": []}


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "hub-agent", "version": VERSION}


@app.get("/agents")
def agents() -> dict:
    data = load_agents()
    items = data.get("agents", [])
    return {"count": len(items), "agents": items}


@app.get("/agents/{agent_id}")
def agent(agent_id: str) -> dict:
    for item in load_agents().get("agents", []):
        if item.get("id") == agent_id:
            return item
    raise HTTPException(status_code=404, detail=f"agent '{agent_id}' not found")


def _llm_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if MASTER_KEY:
        headers["Authorization"] = f"Bearer {MASTER_KEY}"
    return headers


@app.get("/models")
async def models() -> JSONResponse:
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.get(f"{LITELLM_URL}/v1/models", headers=_llm_headers())
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"litellm unreachable: {exc}") from exc
    return JSONResponse(status_code=resp.status_code, content=resp.json())


@app.post("/v1/chat/completions")
async def chat(request: Request) -> Response:
    payload = await request.body()
    async with httpx.AsyncClient(timeout=300) as client:
        try:
            resp = await client.post(
                f"{LITELLM_URL}/v1/chat/completions",
                content=payload,
                headers=_llm_headers(),
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"litellm unreachable: {exc}") from exc
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))
