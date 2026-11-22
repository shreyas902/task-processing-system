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
import ipaddress
import random
import re
import socket
from collections import Counter
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from app.config import get_settings

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


def check_url(url: str) -> None:
    """SSRF guard: workers fetch user-supplied URLs, so refuse internal/private targets.

    Without this, a task could hit cloud metadata (169.254.169.254), the database,
    Redis or other internal services from inside our network. Hosts listed in
    HTTP_ALLOWED_HOSTS bypass the check (e.g. a trusted internal webhook).
    Redirects are not followed, because a public URL could redirect to a private one.
    (A resolver-pinning HTTP transport would also close the DNS-rebinding gap.)
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PermanentError("url must be an absolute http:// or https:// URL")
    host = parsed.hostname.lower()
    allowed = {h.strip().lower() for h in get_settings().http_allowed_hosts.split(",") if h.strip()}
    if host in allowed:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise RuntimeError(f"DNS lookup failed for {host}: {exc}") from exc  # transient => retry
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise PermanentError(f"{host} resolves to non-public address {ip}; blocked (SSRF protection)")


@task("http_check", "GETs a URL and reports status code and latency.", {"url": "https://example.com"})
async def http_check_task(payload: dict) -> dict:
    url = str(payload.get("url", ""))
    await asyncio.to_thread(check_url, url)  # getaddrinfo blocks; keep it off the event loop
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        resp = await client.get(url)
    return {"status_code": resp.status_code, "elapsed_ms": int(resp.elapsed.total_seconds() * 1000)}


HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


@task("http_request",
      "Calls an HTTP endpoint (webhook). 5xx/429/network errors are retried; other 4xx fail permanently.",
      {"method": "POST", "url": "https://httpbin.org/post", "headers": {"X-Source": "task-system"},
       "body": {"hello": "world"}, "expected_status": [200, 201, 202, 204]})
async def http_request_task(payload: dict) -> dict:
    method = str(payload.get("method", "GET")).upper()
    if method not in HTTP_METHODS:
        raise PermanentError(f"method must be one of {sorted(HTTP_METHODS)}")
    url = str(payload.get("url", ""))
    await asyncio.to_thread(check_url, url)
    headers = payload.get("headers") or {}
    if not isinstance(headers, dict):
        raise PermanentError("headers must be an object")
    expected = payload.get("expected_status") or []
    if not isinstance(expected, list) or not all(isinstance(c, int) for c in expected):
        raise PermanentError("expected_status must be a list of integers")
    body = payload.get("body")

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        resp = await client.request(
            method, url, headers={str(k): str(v) for k, v in headers.items()},
            json=body if body is not None and method not in ("GET", "HEAD") else None,
        )
    ok = resp.status_code in expected if expected else resp.is_success
    if not ok:
        msg = f"Unexpected HTTP {resp.status_code} from {url}: {resp.text[:200]}"
        # Client errors won't fix themselves on retry - except timeouts and rate limiting.
        if 400 <= resp.status_code < 500 and resp.status_code not in (408, 425, 429):
            raise PermanentError(msg)
        raise RuntimeError(msg)
    try:
        data = resp.json()
    except ValueError:
        data = resp.text[:1000]
    return {"status_code": resp.status_code, "elapsed_ms": int(resp.elapsed.total_seconds() * 1000),
            "body": data}


async def run_handler(job_type: str, payload: dict) -> Any:
    entry = REGISTRY.get(job_type)
    if entry is None:
        raise PermanentError(f"Unknown job type: {job_type}")
    if entry["is_async"]:
        return await entry["fn"](payload)
    return await asyncio.to_thread(entry["fn"], payload)
