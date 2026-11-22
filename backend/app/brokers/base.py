"""Broker interface.

The broker carries only **job ids**; the job itself lives in PostgreSQL.

Delivery contract (identical for Redis and RabbitMQ):

* ``publish`` enqueues a job id with a priority.
* ``consume`` yields deliveries. A delivery that is not ``ack``-ed (worker
  crashed before claiming the job) is redelivered later - *at-least-once*.
* Workers ``ack`` right after atomically claiming the job in PostgreSQL
  (``UPDATE ... WHERE status='queued'``). From then on, the database lease
  protects the job, so duplicates are harmless: a second claim simply fails.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable


@dataclass
class Delivery:
    job_id: str
    ack: Callable[[], Awaitable[None]]


class Broker(ABC):
    name = "abstract"

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        pass

    @abstractmethod
    async def publish(self, job_id: str, priority: str) -> None: ...

    @abstractmethod
    def consume(self, prefetch: int) -> AsyncIterator[Delivery]: ...

    @abstractmethod
    async def depth(self) -> dict[str, int]:
        """Messages waiting in the broker (by priority when available)."""

    async def requeue_expired(self) -> int:
        """Redeliver messages whose consumer died before acking (no-op if the broker does it natively)."""
        return 0
