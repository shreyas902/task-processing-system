"""Task definitions: user-created, reusable job templates (on-demand or recurring).

A *task* answers "what to run and how": a handler, a default payload and an
execution policy (priority, retries, timeout, optional repeat interval).
A *job* is one execution of it. Jobs created from a task carry ``task_name``,
so every run's status and history can be traced back to its task.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Job, TaskDefinition
from app.jobs import JobService, utcnow

EDITABLE = {"description", "handler", "default_payload", "priority", "max_retries",
            "timeout_s", "interval_s", "enabled"}


class TaskConflict(Exception):
    pass


class TaskService:
    def __init__(self, sessions: async_sessionmaker, jobs: JobService):
        self.sessions = sessions
        self.jobs = jobs

    @staticmethod
    def _schedule(task: TaskDefinition) -> None:
        """(Re)compute next_run_at. The first automatic run is one interval from now."""
        if task.enabled and task.interval_s:
            task.next_run_at = utcnow() + timedelta(seconds=task.interval_s)
        else:
            task.next_run_at = None

    async def create(self, data: dict) -> TaskDefinition:
        task = TaskDefinition(**data, created_at=utcnow(), updated_at=utcnow())
        self._schedule(task)
        async with self.sessions() as s:
            s.add(task)
            try:
                await s.commit()
            except IntegrityError:
                raise TaskConflict(f"A task named '{data['name']}' already exists") from None
        return task

    async def get(self, name: str) -> TaskDefinition | None:
        async with self.sessions() as s:
            return (await s.execute(select(TaskDefinition).where(TaskDefinition.name == name))).scalar_one_or_none()

    async def list(self) -> list[dict]:
        async with self.sessions() as s:
            tasks = (await s.execute(select(TaskDefinition).order_by(TaskDefinition.created_at.desc()))).scalars().all()
            # Per-task run statistics in a single GROUP BY (no N+1 queries).
            counts = (await s.execute(
                select(Job.task_name, Job.status, func.count())
                .where(Job.task_name.is_not(None)).group_by(Job.task_name, Job.status)
            )).all()
        stats: dict[str, dict[str, int]] = {}
        for name, status, n in counts:
            stats.setdefault(name, {})[status] = n
        return [{"task": t, "stats": stats.get(t.name, {})} for t in tasks]

    async def update(self, name: str, changes: dict) -> TaskDefinition:
        async with self.sessions() as s:
            task = (await s.execute(select(TaskDefinition).where(TaskDefinition.name == name)
                                    .with_for_update())).scalar_one_or_none()
            if not task:
                raise KeyError(name)
            for key, value in changes.items():
                if key in EDITABLE:
                    setattr(task, key, value)
            if {"interval_s", "enabled"} & changes.keys():
                self._schedule(task)
            task.updated_at = utcnow()
            await s.commit()
            return task

    async def delete(self, name: str) -> None:
        async with self.sessions() as s:
            res = await s.execute(delete(TaskDefinition).where(TaskDefinition.name == name))
            await s.commit()
        if not res.rowcount:
            raise KeyError(name)
        # Past jobs keep task_name, so their history survives the deletion.

    async def run(self, task: TaskDefinition, payload_override: dict | None = None, delay_s: float = 0,
                  idempotency_key: str | None = None) -> tuple[Job, bool]:
        """Create a job from the task template. Override keys are shallow-merged into the default payload."""
        job, created = await self.jobs.submit(
            type=task.handler, payload={**(task.default_payload or {}), **(payload_override or {})},
            priority=task.priority, max_retries=task.max_retries, timeout_s=task.timeout_s,
            delay_s=delay_s, idempotency_key=idempotency_key, task_name=task.name,
        )
        if created:
            async with self.sessions() as s:
                await s.execute(update(TaskDefinition).where(TaskDefinition.id == task.id)
                                .values(run_count=TaskDefinition.run_count + 1, last_run_at=utcnow()))
                await s.commit()
        return job, created

    async def fire_due(self, batch: int = 100) -> int:
        """Called by the scheduler: start every recurring task whose next_run_at has passed."""
        now = utcnow()
        async with self.sessions() as s:
            due = (await s.execute(
                select(TaskDefinition)
                .where(TaskDefinition.enabled.is_(True), TaskDefinition.interval_s.is_not(None),
                       TaskDefinition.next_run_at <= now)
                .limit(batch).with_for_update(skip_locked=True)
            )).scalars().all()
            slots = {}
            for t in due:
                slots[t.id] = t.next_run_at
                # Advance from *now*, not from the old slot: after downtime we run once,
                # instead of firing a burst of every missed run.
                t.next_run_at = now + timedelta(seconds=t.interval_s)
            await s.commit()
        for t in due:
            # Idempotency key = task + slot: even if two schedulers raced, a slot runs at most once.
            key = f"recurring:{t.id}:{int(slots[t.id].timestamp())}"
            await self.run(t, idempotency_key=key)
        return len(due)
