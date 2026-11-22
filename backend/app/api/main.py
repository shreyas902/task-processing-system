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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text

from app.brokers import create_broker
from app.code_tasks import validate_code_payload, validate_variables
from app.config import get_settings
from app.db import init_db, make_engine, make_sessionmaker
from app.handlers import REGISTRY
from app.jobs import JobConflict, JobService
from app.tasks import TaskConflict, TaskService
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
    sessions = make_sessionmaker(engine)
    app.state.jobs = JobService(sessions, broker, settings)
    app.state.tasks = TaskService(sessions, app.state.jobs)
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
    task_name: str | None = None
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
                    task: str | None = None,
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    items, total = await svc(request).list(status.value if status else None, type, limit, offset, task)
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


# ------------------------------------------------------------ task definitions
TASK_NAME = r"^[a-z0-9][a-z0-9_-]{1,63}$"


def _check_handler_payload(handler: str, payload: dict, recurring: bool = False) -> dict:
    """Validate a task's payload for its handler; returns the (normalised) payload."""
    if handler not in REGISTRY:
        raise ValueError(f"unknown handler; available: {sorted(REGISTRY)}")
    if handler == "run_code":
        validate_code_payload(payload, recurring=recurring)
        return {**payload, "variables": validate_variables(payload.get("variables", []))}
    if handler in ("http_request", "http_check"):
        url = str(payload.get("url", ""))
        if not url.startswith(("http://", "https://")):
            raise ValueError("default_payload.url must start with http:// or https://")
    return payload


def _validate_interval(v: int | None) -> int | None:
    minimum = get_settings().min_task_interval_s
    if v is not None and v < minimum:
        raise ValueError(f"must be at least {minimum} seconds")
    return v


class TaskBase(BaseModel):
    description: str = Field("", max_length=500)
    handler: str = Field(..., examples=["http_request"])
    default_payload: dict[str, Any] = Field(default_factory=dict)
    priority: Priority = Priority.NORMAL
    max_retries: int = Field(3, ge=0, le=20)
    timeout_seconds: int = Field(60, ge=1, le=3600)
    interval_seconds: int | None = Field(None, le=7 * 86_400,
                                         description="Repeat every N seconds; null = run on demand only")
    enabled: bool = True

    @field_validator("interval_seconds")
    @classmethod
    def min_interval(cls, v: int | None) -> int | None:
        return _validate_interval(v)

    @model_validator(mode="after")
    def payload_matches_handler(self):
        self.default_payload = _check_handler_payload(self.handler, self.default_payload,
                                                      recurring=self.interval_seconds is not None)
        return self


class TaskCreate(TaskBase):
    name: str = Field(..., pattern=TASK_NAME, examples=["nightly-report"],
                      description="lowercase letters, digits, '-' and '_'")


class TaskUpdate(BaseModel):
    """PATCH: only the fields that are sent are changed."""
    description: str | None = Field(None, max_length=500)
    handler: str | None = None
    default_payload: dict[str, Any] | None = None
    priority: Priority | None = None
    max_retries: int | None = Field(None, ge=0, le=20)
    timeout_seconds: int | None = Field(None, ge=1, le=3600)
    interval_seconds: int | None = Field(None, le=7 * 86_400)
    enabled: bool | None = None

    @field_validator("interval_seconds")
    @classmethod
    def min_interval(cls, v: int | None) -> int | None:
        return _validate_interval(v)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: str
    handler: str
    default_payload: dict
    priority: str
    max_retries: int
    timeout_s: int
    interval_s: int | None
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    run_count: int
    created_at: datetime
    updated_at: datetime


class TaskWithStats(TaskOut):
    stats: dict[str, int] = {}


class TaskRun(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict, description="Merged over the task's default payload")
    delay_seconds: float = Field(0, ge=0, le=86_400)
    idempotency_key: str | None = Field(None, max_length=128)


def tasks_svc(request: Request) -> TaskService:
    return request.app.state.tasks


def _to_columns(data: dict) -> dict:
    """API field names -> model column names."""
    renames = {"timeout_seconds": "timeout_s", "interval_seconds": "interval_s"}
    out = {renames.get(k, k): v for k, v in data.items()}
    if isinstance(out.get("priority"), Priority):
        out["priority"] = out["priority"].value
    return out


@app.post("/api/tasks", response_model=TaskOut, status_code=201)
async def create_task(request: Request, body: TaskCreate):
    try:
        return await tasks_svc(request).create(_to_columns(body.model_dump()))
    except TaskConflict as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/tasks", response_model=list[TaskWithStats])
async def list_tasks(request: Request):
    rows = await tasks_svc(request).list()
    return [TaskWithStats.model_validate(r["task"]).model_copy(update={"stats": r["stats"]}) for r in rows]


async def _task_or_404(request: Request, name: str):
    task = await tasks_svc(request).get(name)
    if not task:
        raise HTTPException(404, f"Task '{name}' not found")
    return task


@app.get("/api/tasks/{name}", response_model=TaskOut)
async def get_task(request: Request, name: str):
    return await _task_or_404(request, name)


@app.patch("/api/tasks/{name}", response_model=TaskOut)
async def update_task(request: Request, name: str, body: TaskUpdate):
    # Only interval_s may be cleared (null = on-demand only); null elsewhere means "unchanged".
    changes = {k: v for k, v in _to_columns(body.model_dump(exclude_unset=True)).items()
               if v is not None or k == "interval_s"}
    current = await _task_or_404(request, name)
    handler = changes.get("handler") or current.handler
    payload = changes["default_payload"] if changes.get("default_payload") is not None else current.default_payload
    interval = changes["interval_s"] if "interval_s" in changes else current.interval_s
    try:
        normalised = _check_handler_payload(handler, payload, recurring=interval is not None)
        if "default_payload" in changes:
            changes["default_payload"] = normalised
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        return await tasks_svc(request).update(name, changes)
    except KeyError:
        raise HTTPException(404, f"Task '{name}' not found") from None


@app.delete("/api/tasks/{name}", status_code=204)
async def delete_task(request: Request, name: str):
    try:
        await tasks_svc(request).delete(name)
    except KeyError:
        raise HTTPException(404, f"Task '{name}' not found") from None
    return Response(status_code=204)


@app.post("/api/tasks/{name}/run", response_model=JobOut, status_code=201)
async def run_task(request: Request, name: str, response: Response, body: TaskRun | None = None):
    """Run a task now (or after a delay), optionally overriding parts of its payload."""
    body = body or TaskRun()
    task = await _task_or_404(request, name)
    if task.handler == "run_code" and set(body.payload) - {"inputs"}:
        # The code itself is part of the task definition; a run may only supply inputs.
        raise HTTPException(422, "code tasks accept only {\"payload\": {\"inputs\": {...}}} when run")
    payload = {**(task.default_payload or {}), **body.payload}
    try:
        _check_handler_payload(task.handler, payload)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    job, created = await tasks_svc(request).run(task, body.payload, body.delay_seconds, body.idempotency_key)
    if not created:
        response.status_code = 200
    return job
