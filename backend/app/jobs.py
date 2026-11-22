"""Job service: every state transition lives here, shared by API, worker and scheduler.

Concurrency safety comes from **conditional updates** in PostgreSQL::

    UPDATE jobs SET status='running' ... WHERE id=:id AND status='queued' RETURNING *

Only one worker can win that race, so duplicate broker deliveries are safe.
Bulk operations in the scheduler use ``FOR UPDATE SKIP LOCKED`` so several
scheduler replicas never process the same row.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.brokers import Broker
from app.config import Settings
from app.db import Job, JobEvent, Worker
from app.state import CANCELLABLE, RETRYABLE, Status, backoff_delay, should_retry


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobConflict(Exception):
    pass


class JobService:
    def __init__(self, sessions: async_sessionmaker, broker: Broker, settings: Settings):
        self.sessions = sessions
        self.broker = broker
        self.settings = settings

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _event(s: AsyncSession, job_id, event: str, attempt: int = 0,
               worker_id: str | None = None, message: str | None = None) -> None:
        s.add(JobEvent(job_id=job_id, event=event, attempt=attempt, worker_id=worker_id, message=message))

    # ------------------------------------------------------------------- submit
    async def submit(self, *, type: str, payload: dict, priority: str = "normal", max_retries: int = 3,
                     timeout_s: int = 60, delay_s: float = 0, idempotency_key: str | None = None
                     ) -> tuple[Job, bool]:
        """Create a job. Returns (job, created). Re-submitting an idempotency key returns the original job."""
        now = utcnow()
        job = Job(
            id=uuid.uuid4(), type=type, payload=payload, priority=priority, max_retries=max_retries,
            timeout_s=timeout_s, idempotency_key=idempotency_key,
            status=Status.SCHEDULED if delay_s > 0 else Status.QUEUED,
            run_at=now + timedelta(seconds=delay_s), created_at=now, updated_at=now,
        )
        async with self.sessions() as s:
            s.add(job)
            self._event(s, job.id, "created", message=f"priority={priority}")
            if delay_s > 0:
                self._event(s, job.id, "scheduled", message=f"runs at {job.run_at.isoformat()}")
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                existing = (await s.execute(
                    select(Job).where(Job.idempotency_key == idempotency_key))).scalar_one()
                return existing, False
        # Publish AFTER commit: a worker must never receive an id it can't see in the DB yet.
        # If this publish fails, the scheduler's stale-queued sweep republishes it.
        if job.status == Status.QUEUED:
            await self.broker.publish(str(job.id), priority)
        return job, True

    # --------------------------------------------------------------- worker side
    async def claim(self, job_id: str, worker_id: str) -> Job | None:
        """Atomically QUEUED -> RUNNING. None if someone else got it / it was cancelled."""
        try:
            jid = uuid.UUID(job_id)
        except ValueError:
            return None
        now = utcnow()
        async with self.sessions() as s:
            job = (await s.execute(
                update(Job)
                .where(Job.id == jid, Job.status == Status.QUEUED)
                .values(status=Status.RUNNING, attempts=Job.attempts + 1, worker_id=worker_id,
                        started_at=now, updated_at=now, error=None,
                        lease_expires_at=now + func.make_interval(0, 0, 0, 0, 0, 0,
                                                                  Job.timeout_s + self.settings.lease_grace_s))
                .returning(Job)
            )).scalar_one_or_none()
            if job:
                self._event(s, jid, "started", job.attempts, worker_id)
            await s.commit()
            return job

    async def complete(self, job: Job, worker_id: str, result) -> bool:
        now = utcnow()
        async with self.sessions() as s:
            res = await s.execute(
                update(Job)
                .where(Job.id == job.id, Job.status == Status.RUNNING, Job.worker_id == worker_id)
                .values(status=Status.SUCCEEDED, result=result, finished_at=now, updated_at=now,
                        lease_expires_at=None)
            )
            if res.rowcount:
                ms = int((now - job.started_at).total_seconds() * 1000) if job.started_at else None
                self._event(s, job.id, "succeeded", job.attempts, worker_id, f"{ms} ms")
            await s.commit()
            return bool(res.rowcount)

    async def fail(self, job_id, worker_id: str | None, error: str, permanent: bool = False,
                   session: AsyncSession | None = None) -> str | None:
        """Record a failed attempt: schedule a retry with backoff, or dead-letter it."""
        async def _do(s: AsyncSession):
            cond = [Job.id == job_id, Job.status == Status.RUNNING]
            if worker_id:
                cond.append(Job.worker_id == worker_id)
            job = (await s.execute(select(Job).where(*cond).with_for_update())).scalar_one_or_none()
            if not job:
                return None
            now = utcnow()
            job.error = error[:2000]
            job.updated_at = now
            job.lease_expires_at = None
            self._event(s, job.id, "failed", job.attempts, worker_id or job.worker_id, error[:500])
            if not permanent and should_retry(job.attempts, job.max_retries):
                delay = backoff_delay(job.attempts, self.settings.retry_base_delay_s,
                                      self.settings.retry_max_delay_s)
                job.status = Status.SCHEDULED
                job.run_at = now + timedelta(seconds=delay)
                self._event(s, job.id, "retry_scheduled", job.attempts, None,
                            f"retry {job.attempts}/{job.max_retries} in {delay:.1f}s")
            else:
                job.status = Status.DEAD
                job.finished_at = now
                self._event(s, job.id, "dead_lettered", job.attempts, None,
                            "permanent error" if permanent else "retries exhausted")
            return job.status

        if session is not None:
            return await _do(session)
        async with self.sessions() as s:
            status = await _do(s)
            await s.commit()
            return status

    # ------------------------------------------------------------ user actions
    async def cancel(self, job_id: uuid.UUID) -> Job:
        async with self.sessions() as s:
            job = await s.get(Job, job_id, with_for_update=True)
            if not job:
                raise KeyError(job_id)
            if job.status not in CANCELLABLE:
                raise JobConflict(f"Cannot cancel a job that is {job.status}")
            job.status, job.updated_at, job.finished_at = Status.CANCELLED, utcnow(), utcnow()
            self._event(s, job.id, "cancelled", job.attempts)
            await s.commit()
            return job

    async def retry(self, job_id: uuid.UUID) -> Job:
        """Manually re-run a dead-lettered or cancelled job (attempt counter reset)."""
        async with self.sessions() as s:
            job = await s.get(Job, job_id, with_for_update=True)
            if not job:
                raise KeyError(job_id)
            if job.status not in RETRYABLE:
                raise JobConflict(f"Only dead or cancelled jobs can be retried (job is {job.status})")
            job.status, job.attempts, job.error = Status.QUEUED, 0, None
            job.updated_at, job.finished_at, job.run_at = utcnow(), None, utcnow()
            self._event(s, job.id, "manual_retry")
            await s.commit()
        await self.broker.publish(str(job.id), job.priority)
        return job

    async def get(self, job_id: uuid.UUID) -> Job | None:
        async with self.sessions() as s:
            return (await s.execute(
                select(Job).options(selectinload(Job.events)).where(Job.id == job_id))).scalar_one_or_none()

    async def list(self, status: str | None, type: str | None, limit: int, offset: int) -> tuple[list[Job], int]:
        q = select(Job)
        cq = select(func.count()).select_from(Job)
        if status:
            q, cq = q.where(Job.status == status), cq.where(Job.status == status)
        if type:
            q, cq = q.where(Job.type == type), cq.where(Job.type == type)
        async with self.sessions() as s:
            total = (await s.execute(cq)).scalar_one()
            rows = (await s.execute(q.order_by(Job.created_at.desc()).limit(limit).offset(offset))).scalars().all()
        return list(rows), total

    async def stats(self) -> dict:
        now = utcnow()
        async with self.sessions() as s:
            by_status = dict((await s.execute(select(Job.status, func.count()).group_by(Job.status))).all())
            by_type = [
                {"type": t, "status": st, "count": c} for t, st, c in
                (await s.execute(select(Job.type, Job.status, func.count()).group_by(Job.type, Job.status))).all()
            ]
            finished_1m = (await s.execute(select(func.count()).select_from(Job).where(
                Job.finished_at >= now - timedelta(minutes=1), Job.status == Status.SUCCEEDED))).scalar_one()
            avg_ms = (await s.execute(select(
                func.avg(func.extract("epoch", Job.finished_at - Job.started_at) * 1000)
            ).where(Job.status == Status.SUCCEEDED, Job.finished_at >= now - timedelta(minutes=15)))).scalar_one()
            # Throughput time-series: completions per 10s bucket for the last 5 minutes.
            bucket = func.to_timestamp(func.floor(func.extract("epoch", Job.finished_at) / 10) * 10)
            series = (await s.execute(
                select(bucket.label("t"), Job.status, func.count())
                .where(Job.finished_at >= now - timedelta(minutes=5),
                       Job.status.in_([Status.SUCCEEDED, Status.DEAD]))
                .group_by("t", Job.status).order_by("t")
            )).all()
        try:
            depth = await self.broker.depth()
        except Exception as exc:  # noqa: BLE001
            depth = {"error": str(exc)}
        return {
            "by_status": {st.value: by_status.get(st.value, 0) for st in Status},
            "by_type": by_type,
            "succeeded_last_minute": finished_1m,
            "avg_duration_ms_15m": round(float(avg_ms), 1) if avg_ms is not None else None,
            "throughput": [{"t": t.isoformat(), "status": st, "count": c} for t, st, c in series],
            "broker": {"name": self.broker.name, **depth},
        }

    async def workers(self) -> list[dict]:
        async with self.sessions() as s:
            rows = (await s.execute(select(Worker).order_by(Worker.started_at.desc()).limit(50))).scalars().all()
        now = utcnow()
        return [{
            "id": w.id, "hostname": w.hostname, "concurrency": w.concurrency, "active": w.active,
            "processed": w.processed, "failed": w.failed, "started_at": w.started_at,
            "last_heartbeat": w.last_heartbeat,
            "alive": not w.stopped and (now - w.last_heartbeat).total_seconds() < 3 * self.settings.worker_heartbeat_s,
        } for w in rows]
