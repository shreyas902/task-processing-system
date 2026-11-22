"""Scheduler / reaper process.

    python -m app.scheduler

Runs a loop every ``SCHEDULER_INTERVAL_S`` that:

1. **Promotes due jobs** - SCHEDULED jobs whose ``run_at`` has passed (delayed
   jobs and retries waiting out their backoff) become QUEUED and are published.
2. **Reaps expired leases** - RUNNING jobs whose worker vanished (crash, OOM,
   network partition) are failed like any other attempt => retry or dead-letter.
3. **Republishes stale QUEUED jobs** - covers the gap where the DB commit
   succeeded but the broker publish failed (the "dual-write" problem).
4. **Requeues expired in-flight messages** (Redis only; RabbitMQ does it natively).
5. **Fires recurring tasks** - user-created tasks with a repeat interval whose
   ``next_run_at`` has passed get a new job.

High availability: you can run several schedulers. Only the one holding the
PostgreSQL advisory lock does work (leader election); the others stand by and
take over if the leader's connection dies. Row-level work uses
``FOR UPDATE SKIP LOCKED`` as a second line of defence.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import timedelta

from sqlalchemy import select, text, update

from app.brokers import create_broker
from app.config import get_settings
from app.db import Job, JobEvent, init_db, make_engine, make_sessionmaker
from app.jobs import JobService, utcnow
from app.tasks import TaskService
from app.state import Status

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [scheduler] %(message)s")
log = logging.getLogger("scheduler")

LEADER_LOCK_ID = 77_001
BATCH = 200


class Scheduler:
    def __init__(self):
        self.settings = get_settings()
        self.engine = make_engine(self.settings.database_url)
        self.sessions = make_sessionmaker(self.engine)
        self.broker = create_broker(self.settings)
        self.jobs = JobService(self.sessions, self.broker, self.settings)
        self.tasks = TaskService(self.sessions, self.jobs)
        self.stopping = asyncio.Event()

    async def promote_due(self) -> int:
        now = utcnow()
        async with self.sessions() as s:
            due = select(Job.id).where(Job.status == Status.SCHEDULED, Job.run_at <= now) \
                .order_by(Job.run_at).limit(BATCH).with_for_update(skip_locked=True)
            rows = (await s.execute(
                update(Job).where(Job.id.in_(due.scalar_subquery()))
                .values(status=Status.QUEUED, updated_at=now)
                .returning(Job.id, Job.priority, Job.attempts)
            )).all()
            for jid, _, attempt in rows:
                s.add(JobEvent(job_id=jid, event="queued", attempt=attempt))
            await s.commit()
        for jid, priority, _ in rows:
            await self.broker.publish(str(jid), priority)
        return len(rows)

    async def reap_expired_leases(self) -> int:
        async with self.sessions() as s:
            expired = (await s.execute(
                select(Job.id).where(Job.status == Status.RUNNING, Job.lease_expires_at < utcnow())
                .limit(BATCH).with_for_update(skip_locked=True)
            )).scalars().all()
            for jid in expired:
                await self.jobs.fail(jid, None, "Lease expired: worker died or stopped responding", session=s)
            await s.commit()
        return len(expired)

    async def republish_stale_queued(self) -> int:
        now = utcnow()
        cutoff = now - timedelta(seconds=self.settings.stale_queued_s)
        async with self.sessions() as s:
            rows = (await s.execute(
                update(Job).where(Job.id.in_(
                    select(Job.id).where(Job.status == Status.QUEUED, Job.updated_at < cutoff)
                    .limit(BATCH).with_for_update(skip_locked=True).scalar_subquery()))
                .values(updated_at=now)  # bump so we don't republish again on the next tick
                .returning(Job.id, Job.priority)
            )).all()
            await s.commit()
        for jid, priority in rows:
            await self.broker.publish(str(jid), priority)
        return len(rows)

    async def tick(self) -> None:
        promoted = await self.promote_due()
        reaped = await self.reap_expired_leases()
        stale = await self.republish_stale_queued()
        requeued = await self.broker.requeue_expired()
        fired = await self.tasks.fire_due()
        if promoted or reaped or stale or requeued or fired:
            log.info("promoted=%d reaped=%d republished=%d requeued=%d recurring_fired=%d",
                     promoted, reaped, stale, requeued, fired)

    async def run(self) -> None:
        await init_db(self.engine)
        await self.broker.connect()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)

        # Leader election: hold a session-level advisory lock on a dedicated connection.
        async with self.engine.connect() as lock_conn:
            while not self.stopping.is_set():
                got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                                               {"k": LEADER_LOCK_ID})).scalar()
                await lock_conn.commit()
                if got:
                    break
                log.info("another scheduler is leader; standing by")
                await self._sleep(5)
            log.info("scheduler is leader (broker=%s)", self.broker.name)
            while not self.stopping.is_set():
                try:
                    await self.tick()
                except Exception:  # noqa: BLE001 - keep the loop alive
                    log.exception("scheduler tick failed")
                await self._sleep(self.settings.scheduler_interval_s)
        await self.broker.close()
        await self.engine.dispose()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stopping.wait(), seconds)
        except asyncio.TimeoutError:
            pass


if __name__ == "__main__":
    asyncio.run(Scheduler().run())
