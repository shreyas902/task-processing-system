"""Task handlers: the actual work a job performs, looked up by ``job.type``.

Handlers receive the job payload (a dict) and return a JSON-serialisable
result. Raising an exception marks the attempt as failed (and triggers a retry
if attempts remain). ``PermanentError`` skips retries - use it for failures
that will never succeed (e.g. invalid input).

Sync handlers run in a thread pool so they don't block the worker's event loop;
CPU-heavy work in production would go to a process pool or a dedicated worker.
"""
from __future__ import annotations

import asyncio
import random
import re
from collections import Counter
from typing import Any, Awaitable, Callable

import httpx

Handler = Callable[[dict], Any] | Callable[[dict], Awaitable[Any]]
REGISTRY: dict[str, dict] = {}


class PermanentError(Exception):
    """Non-retryable failure: the job goes straight to the dead-letter queue."""


def task(name: str, description: str, example: dict):
    def register(fn: Handler) -> Handler:
        REGISTRY[name] = {"fn": fn, "description": description, "example": example,
                          "is_async": asyncio.iscoroutinefunction(fn)}
        return fn
    return register


@task("sleep", "Waits N seconds - simulates slow I/O.", {"seconds": 3})
async def sleep_task(payload: dict) -> dict:
    seconds = float(payload.get("seconds", 1))
    await asyncio.sleep(seconds)
    return {"slept": seconds}


@task("fibonacci", "Computes the n-th Fibonacci number (CPU bound, runs in a thread).", {"n": 30})
def fibonacci_task(payload: dict) -> dict:
    n = int(payload.get("n", 20))
    if n < 0 or n > 10_000:
        raise PermanentError("n must be between 0 and 10000")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return {"n": n, "digits": len(str(a)), "value": str(a) if len(str(a)) <= 50 else str(a)[:50] + "…"}


@task("word_count", "Counts words in a text and returns the most common ones.",
      {"text": "the quick brown fox jumps over the lazy dog the end"})
def word_count_task(payload: dict) -> dict:
    words = re.findall(r"[a-zA-Z']+", str(payload.get("text", "")).lower())
    return {"words": len(words), "top": Counter(words).most_common(5)}


@task("flaky", "Fails randomly with probability fail_rate - demonstrates retries with backoff.",
      {"fail_rate": 0.6})
async def flaky_task(payload: dict) -> dict:
    await asyncio.sleep(0.5)
    if random.random() < float(payload.get("fail_rate", 0.5)):
        raise RuntimeError("transient failure (simulated)")
    return {"ok": True}


@task("always_fail", "Always fails - ends up in the dead-letter queue.", {})
async def always_fail_task(payload: dict) -> dict:
    await asyncio.sleep(0.2)
    raise RuntimeError("this job always fails")


@task("http_check", "GETs a URL and reports status code and latency.", {"url": "https://example.com"})
async def http_check_task(payload: dict) -> dict:
    url = str(payload.get("url", ""))
    if not url.startswith(("http://", "https://")):
        raise PermanentError("url must start with http:// or https://")
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.get(url)
    return {"status_code": resp.status_code, "elapsed_ms": int(resp.elapsed.total_seconds() * 1000)}


async def run_handler(job_type: str, payload: dict) -> Any:
    entry = REGISTRY.get(job_type)
    if entry is None:
        raise PermanentError(f"Unknown job type: {job_type}")
    if entry["is_async"]:
        return await entry["fn"](payload)
    return await asyncio.to_thread(entry["fn"], payload)
