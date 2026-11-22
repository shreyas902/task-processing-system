# Design decisions & trade-offs

## 1. PostgreSQL is the source of truth, the broker carries only ids
Job state, results and history live in PostgreSQL, which is transactional, queryable and durable. The broker
only answers "which job should a worker look at next?". As a result:
- the broker can lose a message and nothing is lost, because the scheduler republishes from the DB,
- the dashboard can query jobs, filter them and page through them, which a queue can't do,
- switching between Redis and RabbitMQ is a single interface (`brokers/base.py`).

## 2. Delivery semantics: at-least-once delivery, exactly-once *claim*
A message can be delivered twice (redelivery after a crash, or a republish). Workers therefore claim jobs with
an atomic conditional update:

```sql
UPDATE jobs SET status='running', attempts=attempts+1, worker_id=:me, lease_expires_at=now()+timeout+grace
WHERE id=:id AND status='queued'
RETURNING *;
```

Only one worker can win. The losers ack and drop the message. The worker **acks after claiming**:
- crash before the claim → the message is redelivered (Redis visibility timeout or RabbitMQ unacked redelivery),
- crash after the claim → the DB lease expires and the scheduler retries the job.

The *handler* itself can still run more than once (crash after side effects, before the result is saved), so
handlers that touch external systems should be idempotent. Exactly-once execution is impossible
in general across a network.

## 3. The dual-write problem (DB commit + broker publish)
`submit` commits first and publishes afterwards. Publishing first would let a worker receive an id it can't
see yet. If the publish fails after the commit, the job stays `queued` with no message, so the scheduler republishes
`queued` jobs that haven't been claimed for `STALE_QUEUED_S`. The production-grade version of this is the
**transactional outbox** pattern: write an outbox row in the same transaction, and have a relay publish it.

## 4. Redis broker
- **Lists per priority.** `LPUSH` to enqueue and `RPOP` to dequeue, which is FIFO.
- **Atomic multi-list pop.** A Lua script pops from the first non-empty list (high → normal → low) **and**
  adds the id to the `tq:inflight` ZSET with score = visibility deadline, in one atomic step. `BLMOVE`
  works on a single list only, so we poll every 200 ms when idle.
- **Visibility timeout** (like SQS). If an entry stays in-flight past its deadline, the scheduler puts it back
  at the front of its list.
- **Durability.** AOF persistence is enabled. Even if Redis is wiped, the DB-driven republish restores the queue.
- **Strict priority** can starve `low` under sustained load. Alternatives are weighted polling or aging.

## 5. RabbitMQ broker
- Durable queue with `x-max-priority=10`, persistent messages, and publisher confirms.
- `prefetch_count = worker concurrency` gives back-pressure and fair dispatch.
- Manual ack: if a consumer dies, its unacked messages are redelivered automatically, so no reaper is needed.
- Retries/backoff are scheduled in the DB, not with per-message TTL + DLX. That avoids head-of-line blocking
  with mixed TTLs and keeps one retry implementation for both brokers.

## 6. Retries
- `delay = min(cap, base · 2^(attempt-1)) · (1 ± 20%)`. The exponential part stops hammering a failing
  dependency, and the jitter stops thousands of jobs retrying at the same instant (thundering herd).
- `max_retries` = retries after the first attempt, so `attempts ≤ max_retries + 1`.
- `PermanentError` (validation, unknown type) goes straight to the DLQ, because retrying can't help.
- Timeouts (`asyncio.wait_for`) and lease expiry (crashed worker) count as failed attempts.

## 7. Dead-letter queue
A DLQ is just `status='dead'`. It keeps the error, the full event history and the payload, and it can be
queried, alerted on, and retried by hand (`POST /jobs/{id}/retry` resets attempts).

## 8. Scheduler
- Promotes due `scheduled` jobs (delayed jobs and retries in backoff) to `queued` and publishes them.
- Reaps `running` jobs whose lease expired.
- Republishes stale `queued` jobs, and requeues expired Redis in-flight entries.
- **Leader election** with `pg_try_advisory_lock` on a dedicated connection. You can run several replicas.
  If the leader's connection dies, Postgres releases the lock and a standby takes over.
- Row batches use `FOR UPDATE SKIP LOCKED`, so concurrent processors never block each other or double-process rows.
- Partial indexes (`WHERE status='scheduled'`, `WHERE status='running'`) keep these polling queries cheap even
  with millions of finished rows.

## 9. Worker
- **asyncio** event loop. Concurrency is bounded by a semaphore that is acquired *before* pulling a message,
  so no message waits in a worker's hands.
- I/O-bound handlers are `async`. Sync/CPU handlers run via `asyncio.to_thread`. For heavy CPU work
  you would use a process pool or dedicated worker pools per job type, because of the GIL.
- Heartbeats upsert into the `workers` table (`INSERT ... ON CONFLICT DO UPDATE`).
- **Graceful shutdown** on SIGTERM: stop consuming, wait for running jobs (`stop_grace_period`), then mark the worker stopped.

## 10. User-created tasks
- **Task vs job.** A *task definition* is a template: what to run (handler + default payload) and how (priority,
  retries, timeout, optional interval). A *job* is one execution. Running a task just calls `JobService.submit`
  with `task_name` set, so tasks reuse the whole execution engine (retries, DLQ, leases, history) with no
  special cases in the worker.
- **No user code execution.** Users compose *registered* handlers. The generic `http_request` webhook handler covers
  most custom needs without running arbitrary code (which would need sandboxing).
- **Recurring tasks.** The scheduler (single leader) selects due definitions with `FOR UPDATE SKIP LOCKED`
  and advances `next_run_at` **from now**, not from the missed slot. After downtime a task runs once instead of
  replaying every missed run. Each run gets the idempotency key `recurring:{task_id}:{slot}`, so a slot can never
  produce two jobs. A partial index on `next_run_at WHERE enabled AND interval_s IS NOT NULL` keeps polling cheap.
  The minimum interval (5 s) protects the system and downstream services.
- **Webhook retry semantics.** 5xx, 408/425/429 and network errors are transient, so they retry with backoff. Other 4xx
  responses mean the request itself is wrong, so it goes straight to the DLQ (`PermanentError`) without wasting retries.
- **SSRF protection.** Workers sit inside the network, so a user-supplied URL could target cloud metadata
  (`169.254.169.254`), Postgres, Redis or admin APIs. Before every request, the hostname is resolved and rejected if
  any address is non-global. Redirects are disabled, because a public URL could 302 to an internal one.
  `HTTP_ALLOWED_HOSTS` allowlists trusted internal hosts. Remaining gap: DNS rebinding between the check and
  the connect. The fix is to pin the resolved IP in a custom transport, or route through an egress proxy.
- **Schema evolution.** `create_all` doesn't alter existing tables, so the new `jobs.task_name` column is added with
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. In production this would be an Alembic migration.

## 11. Python / shell code tasks (sandboxed)
Running user code is remote code execution by design, so the design starts from "assume the code is hostile":

| Threat | Mitigation |
|---|---|
| Stealing DB/broker credentials | Code never runs in a worker. The `runner` container's env has no secrets, and the subprocess gets a fresh env (PATH, HOME, LANG and inputs only) |
| Attacking Postgres/Redis/internal APIs, exfiltrating data | The runner is only on an `internal: true` Docker network shared with workers: no route to the DB, the broker or the internet |
| Tampering with the runner | Read-only root FS (scratch is a 64 MB tmpfs), non-root user, `cap_drop: ALL`, `no-new-privileges` |
| Resource exhaustion | rlimits per script (CPU seconds, 256 MB address space, 16 MB files, 256 fds), `pids_limit` (fork bombs), container mem/cpu caps, runner concurrency semaphore, 64 KB output cap |
| Runaway / background processes | Each script runs in its own session/process group. The whole group is SIGKILLed on timeout **and** after exit (kills `cmd &` leftovers) |
| Shell injection through inputs | Inputs are passed as env vars and a JSON `inputs` dict, never interpolated into code. Names must be lowercase identifiers, so they can't override `PATH`/`LD_PRELOAD` |
| Unauthorized calls to the runner | Shared bearer token (constant-time compare), reachable only from the sandbox network |

Other decisions:
- **Variables** are declared per task (type, default, required, description), validated and coerced in the API (422 before
  any job exists) and again in the worker and runner (defence in depth). Recurring tasks must have defaults for required variables.
- **A run may only supply `inputs`**. The code is part of the task definition, so a run can't swap it.
- **Results.** stdout/stderr/exit code are always captured. Python can set `result` for structured output (via a `runpy` wrapper,
  which keeps the user's line numbers in tracebacks). A non-zero exit or a timeout fails the attempt, with the stderr/stdout tail in `error`.
- The script timeout is slightly shorter than the job timeout, so the sandbox kills the script and returns partial output
  before the worker's own timeout fires.
- **Stronger isolation for multi-tenant use:** gVisor / Kata / Firecracker microVMs, or a fresh container per run,
  seccomp profiles, and per-tenant quotas. A container shares the host kernel, so a kernel exploit could escape it.

## 12. Measured
- Both brokers pass the same end-to-end suite.
- `SIGKILL` on all workers mid-job: every job was recovered and succeeded on attempt 2.
- 100 ms jobs: 2 workers × 4 slots ≈ 60 jobs/s, and 4 workers ≈ 119 jobs/s (linear scale-out). The ceiling is
  slots / job duration, and the rest is DB round-trips per job (claim, complete, events).

## Next steps
- Transactional outbox, plus Alembic migrations.
- Per-type queues and rate limits, cron expressions (beyond fixed intervals), and job dependencies (DAGs).
- Prometheus metrics, OpenTelemetry tracing, and auth / multi-tenancy.
- Archival/partitioning of old `jobs` and `job_events` rows.
