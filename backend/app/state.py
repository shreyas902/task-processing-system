"""Job lifecycle (a small state machine) and the retry policy.

::

    create ──► SCHEDULED ──(run_at due)──► QUEUED ──(claimed)──► RUNNING ──► SUCCEEDED
       │           ▲                         ▲                     │
       └──────────►│ (no delay)              │ manual retry        ├─ fail, attempts left ─► SCHEDULED (backoff)
                   │                         │                     └─ fail, no attempts  ─► DEAD (dead-letter)
                   └── retry backoff ◄───────┘
    SCHEDULED / QUEUED ──cancel──► CANCELLED          DEAD / CANCELLED ──retry──► QUEUED
"""
from __future__ import annotations

import random
from enum import Enum


class Status(str, Enum):
    SCHEDULED = "scheduled"   # waiting for run_at (delayed job or retry backoff)
    QUEUED = "queued"         # handed to the broker, waiting for a worker
    RUNNING = "running"       # claimed by a worker (has a lease)
    SUCCEEDED = "succeeded"
    DEAD = "dead"             # failed permanently - the dead-letter queue
    CANCELLED = "cancelled"


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


TERMINAL = {Status.SUCCEEDED, Status.DEAD, Status.CANCELLED}
CANCELLABLE = {Status.SCHEDULED, Status.QUEUED}
RETRYABLE = {Status.DEAD, Status.CANCELLED}

# RabbitMQ message priority (queue declared with x-max-priority=10).
AMQP_PRIORITY = {Priority.HIGH: 9, Priority.NORMAL: 5, Priority.LOW: 1}


def backoff_delay(attempt: int, base: float, cap: float, jitter: float = 0.2,
                  rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter.

    ``attempt`` is the attempt that just failed (1-based). The jitter spreads
    retries out so that many jobs failing at once (e.g. a downstream outage)
    don't all retry at the same instant (thundering herd).
    """
    rng = rng or random
    delay = min(cap, base * (2 ** max(attempt - 1, 0)))
    return max(0.0, delay * (1 + rng.uniform(-jitter, jitter)))


def should_retry(attempts: int, max_retries: int) -> bool:
    """``attempts`` counts executions so far; 1 initial run + ``max_retries`` retries."""
    return attempts <= max_retries
