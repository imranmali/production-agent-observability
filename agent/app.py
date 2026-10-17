"""HTTP API.

POST /v1/chat     run the agent on one customer message
GET  /healthz     liveness (process is up)
GET  /readyz      readiness (prompt loaded, provider configured)
GET  /version     release, track, model, prompt version
GET  /metrics     Prometheus exposition
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from agent.config import get_settings
from agent.llm import make_client
from agent.runner import Agent
from agent.telemetry.tracing import setup_tracing


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        try:
            body = json.loads(msg) if msg.startswith("{") else {"msg": msg}
        except json.JSONDecodeError:
            body = {"msg": msg}
        out = {"ts": self.formatTime(record), "level": record.levelname, "logger": record.name, **body}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), handlers=[_handler])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = Field(None, max_length=128)
    user_id: str | None = Field(None, max_length=128)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    provider = setup_tracing(settings)
    app.state.settings = settings
    app.state.agent = Agent(settings, make_client(settings))
    yield
    provider.shutdown()


app = FastAPI(title="Support agent", lifespan=lifespan)


@app.post("/v1/chat")
async def chat(req: ChatRequest, response: Response) -> dict:
    result = await app.state.agent.run(req.message, session_id=req.session_id, user_id=req.user_id)
    s = app.state.settings
    response.headers["x-agent-release"] = s.release
    response.headers["x-agent-track"] = s.deployment_track
    response.headers["x-trace-id"] = result.trace_id
    if result.outcome in ("provider_error", "error"):
        response.status_code = 503 if result.outcome == "provider_error" else 500
    return result.to_dict()


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(response: Response) -> dict:
    s = app.state.settings
    if s.llm_provider == "anthropic" and not (
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    ):
        response.status_code = 503
        return {"status": "not ready", "reason": "no Anthropic credentials"}
    return {"status": "ready"}


@app.get("/version")
async def version() -> dict:
    s = app.state.settings
    return {
        "release": s.release,
        "track": s.deployment_track,
        "provider": s.llm_provider,
        "model": s.model,
        "prompt_version": s.prompt_version,
    }


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
