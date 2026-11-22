"""Worker process: consume job ids from the broker and execute them concurrently.

    python -m app.worker

* Concurrency is bounded by an ``asyncio.Semaphore`` (and the RabbitMQ prefetch).
* Each job runs under ``asyncio.wait_for(timeout)``; a hung job fails instead of
  blocking the slot forever.
* Heartbeats (every few seconds) upsert this worker's row so the dashboard can
  show live workers.
* SIGTERM/SIGINT => graceful shutdown: stop taking new work, let in-flight jobs
  finish (up to ``SHUTDOWN_GRACE_S``), then exit. Jobs still running after that
  are recovered by the scheduler when their lease expires.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid

from sqlalchemy.dialects.postgresql import insert

from app.brokers import create_broker
from app.config import get_settings
from app.db import Worker, init_db, make_engine, make_sessionmaker
from app.handlers import PermanentError, run_handler
from app.jobs import JobService, utcnow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [worker] %(message)s")
log = logging.getLogger("worker")


class WorkerProcess:
    def __init__(self):
        self.settings = get_settings()
        self.id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:4]}"
        self.engine = make_engine(self.settings.database_url)
        self.sessions = make_sessionmaker(self.engine)
        self.broker = create_broker(self.settings)
        self.jobs = JobService(self.sessions, self.broker, self.settings)
        self.sem = asyncio.Semaphore(self.settings.worker_concurrency)
        self.tasks: set[asyncio.Task] = set()
        self.stopping = asyncio.Event()
        self.processed = 0
        self.failed = 0

    async def heartbeat(self, stopped: bool = False) -> None:
        values = dict(id=self.id, hostname=socket.gethostname(), concurrency=self.settings.worker_concurrency,
                      active=len(self.tasks), processed=self.processed, failed=self.failed,
                      last_heartbeat=utcnow(), stopped=int(stopped))
        stmt = insert(Worker).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=[Worker.id],
                                          set_={k: v for k, v in values.items() if k != "id"})
        async with self.sessions() as s:
            await s.execute(stmt)
            await s.commit()

    async def heartbeat_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                await self.heartbeat()
            except Exception as exc:  # noqa: BLE001
                log.warning("heartbeat failed: %s", exc)
            try:
                await asyncio.wait_for(self.stopping.wait(), self.settings.worker_heartbeat_s)
            except asyncio.TimeoutError:
                pass

    async def execute(self, job) -> None:
        try:
            log.info("job %s (%s) attempt %d started", job.id, job.type, job.attempts)
            result = await asyncio.wait_for(run_handler(job.type, job.payload or {}, job.timeout_s),
                                          timeout=job.timeout_s)
            await self.jobs.complete(job, self.id, result)
            self.processed += 1
            log.info("job %s succeeded", job.id)
        except asyncio.TimeoutError:
            self.failed += 1
            status = await self.jobs.fail(job.id, self.id, f"Timed out after {job.timeout_s}s")
            log.warning("job %s timed out -> %s", job.id, status)
        except PermanentError as exc:
            self.failed += 1
            await self.jobs.fail(job.id, self.id, f"PermanentError: {exc}", permanent=True)
            log.warning("job %s permanent failure: %s", job.id, exc)
        except Exception as exc:  # noqa: BLE001 - any handler error is a failed attempt
            self.failed += 1
            status = await self.jobs.fail(job.id, self.id, f"{type(exc).__name__}: {exc}")
            log.warning("job %s failed (%s) -> %s", job.id, exc, status)
        finally:
            self.sem.release()

    async def consume_loop(self) -> None:
        deliveries = self.broker.consume(prefetch=self.settings.worker_concurrency)
        while not self.stopping.is_set():
            await self.sem.acquire()  # back-pressure: only take a message when a slot is free
            delivery = await anext(deliveries)
            job = await self.jobs.claim(delivery.job_id, self.id)
            await delivery.ack()  # ack after claim: the DB lease protects the job from here on
            if job is None:
                self.sem.release()  # duplicate delivery, cancelled job, or already taken
                continue
            t = asyncio.create_task(self.execute(job))
            self.tasks.add(t)
            t.add_done_callback(self.tasks.discard)

    async def run(self) -> None:
        await init_db(self.engine)
        await self.broker.connect()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.stopping.set)
        log.info("worker %s started (broker=%s, concurrency=%d)",
                 self.id, self.broker.name, self.settings.worker_concurrency)

        hb = asyncio.create_task(self.heartbeat_loop())
        consumer = asyncio.create_task(self.consume_loop())
        await self.stopping.wait()

        log.info("shutting down: waiting for %d running job(s)", len(self.tasks))
        consumer.cancel()
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=self.settings.shutdown_grace_s)
        await hb
        await self.heartbeat(stopped=True)
        await self.broker.close()
        await self.engine.dispose()
        log.info("worker %s stopped", self.id)


if __name__ == "__main__":
    asyncio.run(WorkerProcess().run())
