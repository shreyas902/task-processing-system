"""REST API for submitting and inspecting jobs.

    uvicorn app.api.main:app
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from app.brokers import create_broker
from app.config import get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.handlers import REGISTRY
from app.jobs import JobConflict, JobService
from app.state import Priority, Status


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = make_engine(settings.database_url)
    await init_db(engine)
    broker = create_broker(settings)
    await broker.connect()
    app.state.engine = engine
    app.state.broker = broker
    app.state.jobs = JobService(make_sessionmaker(engine), broker, settings)
    yield
    await broker.close()
    await engine.dispose()


app = FastAPI(title="Distributed Task Processing System", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins.split(","),
                   allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------- schemas
class JobCreate(BaseModel):
    type: str = Field(..., examples=["sleep"])
    payload: dict[str, Any] = Field(default_factory=dict, examples=[{"seconds": 2}])
    priority: Priority = Priority.NORMAL
    max_retries: int = Field(3, ge=0, le=20)
    timeout_seconds: int = Field(60, ge=1, le=3600)
    delay_seconds: float = Field(0, ge=0, le=86_400)
    idempotency_key: str | None = Field(None, max_length=128)

    @field_validator("type")
    @classmethod
    def known_type(cls, v: str) -> str:
        if v not in REGISTRY:
            raise ValueError(f"unknown job type; available: {sorted(REGISTRY)}")
        return v


class BulkCreate(JobCreate):
    count: int = Field(10, ge=1, le=1000)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    ts: datetime
    event: str
    attempt: int
    worker_id: str | None
    message: str | None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    type: str
    payload: dict
    priority: str
    status: str
    attempts: int
    max_retries: int
    timeout_s: int
    idempotency_key: str | None
    result: Any | None
    error: str | None
    worker_id: str | None
    created_at: datetime
    run_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None


class JobDetail(JobOut):
    events: list[EventOut] = []


class JobPage(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int


def svc(request: Request) -> JobService:
    return request.app.state.jobs


async def _submit(request: Request, body: JobCreate):
    return await svc(request).submit(
        type=body.type, payload=body.payload, priority=body.priority.value, max_retries=body.max_retries,
        timeout_s=body.timeout_seconds, delay_s=body.delay_seconds, idempotency_key=body.idempotency_key)


# ---------------------------------------------------------------------- routes
@app.get("/api/health")
async def health(request: Request):
    async with request.app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    await request.app.state.broker.depth()
    return {"status": "ok", "broker": request.app.state.broker.name}


@app.get("/api/task-types")
async def task_types():
    return [{"type": k, "description": v["description"], "example": v["example"]} for k, v in REGISTRY.items()]


@app.post("/api/jobs", response_model=JobOut, status_code=201)
async def create_job(request: Request, body: JobCreate, response: Response):
    job, created = await _submit(request, body)
    if not created:
        response.status_code = 200  # idempotent replay: same key returns the original job
    return job


@app.post("/api/jobs/bulk", status_code=201)
async def create_bulk(request: Request, body: BulkCreate):
    """Submit many jobs at once - handy for load-testing the workers."""
    ids = []
    for i in range(body.count):
        data = body.model_copy(update={"idempotency_key": f"{body.idempotency_key}-{i}" if body.idempotency_key else None})
        job, _ = await _submit(request, data)
        ids.append(str(job.id))
    return {"created": len(ids), "ids": ids}


@app.get("/api/jobs", response_model=JobPage)
async def list_jobs(request: Request, status: Status | None = None, type: str | None = None,
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    items, total = await svc(request).list(status.value if status else None, type, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
async def get_job(request: Request, job_id: uuid.UUID):
    job = await svc(request).get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return job


@app.post("/api/jobs/{job_id}/cancel", response_model=JobOut)
async def cancel_job(request: Request, job_id: uuid.UUID):
    try:
        return await svc(request).cancel(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found") from None
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(request: Request, job_id: uuid.UUID):
    try:
        return await svc(request).retry(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found") from None
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/dead-letter", response_model=JobPage)
async def dead_letter(request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    items, total = await svc(request).list(Status.DEAD.value, None, limit, offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/stats")
async def stats(request: Request):
    return await svc(request).stats()


@app.get("/api/workers")
async def workers(request: Request):
    return await svc(request).workers()
