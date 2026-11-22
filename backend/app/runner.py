"""Sandbox runner service: executes code tasks, isolated from the rest of the system.

    uvicorn app.runner:app --port 9000

Deployed as its own container with no secrets, a read-only filesystem and an
internal-only network (see docker-compose.yml). Workers call ``POST /run``; the
code never runs inside a worker, which holds the database credentials.
"""
from __future__ import annotations

import asyncio
import hmac
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.code_tasks import MAX_CODE_CHARS, resolve_inputs
from app.config import get_settings
from app.sandbox import execute

settings = get_settings()
app = FastAPI(title="Sandbox runner", docs_url=None, redoc_url=None)
slots = asyncio.Semaphore(settings.runner_max_concurrency)


class RunRequest(BaseModel):
    language: Literal["python", "shell"]
    code: str = Field(..., min_length=1, max_length=MAX_CODE_CHARS)
    inputs: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    timeout_s: float = Field(30, gt=0, le=3600)


@app.get("/health")
async def health():
    return {"status": "ok", "busy": settings.runner_max_concurrency - slots._value}


@app.post("/run")
async def run(req: RunRequest, authorization: str = Header("")):
    # Constant-time comparison: don't leak the token through response timing.
    if not hmac.compare_digest(authorization.encode(), f"Bearer {settings.runner_token}".encode()):
        raise HTTPException(401, "invalid runner token")
    try:
        inputs = resolve_inputs([], req.inputs)  # names/values re-validated here too (defence in depth)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    async with slots:
        return await execute(req.language, req.code, inputs, req.timeout_s,
                             memory_mb=settings.runner_memory_mb, max_output=settings.runner_max_output)
