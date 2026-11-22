"""Database models (PostgreSQL via SQLAlchemy 2.0 async + asyncpg).

PostgreSQL is the **source of truth** for job state. The broker (Redis or
RabbitMQ) only transports job *ids* to workers; if the broker loses a message
the scheduler can always rebuild the queue from the database.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict)
    priority: Mapped[str] = mapped_column(String(8), default="normal")
    status: Mapped[str] = mapped_column(String(16), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    timeout_s: Mapped[int] = mapped_column(Integer, default=60)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    result: Mapped[dict | None] = mapped_column(JsonType)
    error: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["JobEvent"]] = relationship(
        back_populates="job", order_by="JobEvent.id", cascade="all, delete-orphan")

    __table_args__ = (
        # Partial indexes keep the scheduler's polling queries cheap even with millions of finished jobs.
        Index("ix_jobs_due", "run_at", postgresql_where=text("status = 'scheduled'")),
        Index("ix_jobs_lease", "lease_expires_at", postgresql_where=text("status = 'running'")),
        Index("ix_jobs_created", "created_at"),
    )


class JobEvent(Base):
    """Append-only execution history (audit log) for a job."""
    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event: Mapped[str] = mapped_column(String(32))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)

    job: Mapped[Job] = relationship(back_populates="events")


class Worker(Base):
    """Worker registry, refreshed by heartbeats. A worker is alive if its heartbeat is recent."""
    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(128))
    concurrency: Mapped[int] = mapped_column(Integer)
    active: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stopped: Mapped[int] = mapped_column(Integer, default=0)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_size=10, max_overflow=10, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Create tables. (In a long-lived production system you'd use Alembic migrations.)

    Several services start at once, so serialise DDL with an advisory lock.
    """
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(4242)"))
        await conn.run_sync(Base.metadata.create_all)
