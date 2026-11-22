"""Redis broker: one list per priority + an "in-flight" sorted set.

Keys::

    tq:queue:high | tq:queue:normal | tq:queue:low   LIST  (LPUSH to enqueue, RPOP to dequeue -> FIFO)
    tq:inflight                                      ZSET  member=job_id, score=visibility deadline

Dequeue is a Lua script, so "pop from the highest non-empty priority list and
record it as in-flight" happens **atomically** inside Redis - a worker crash
can't lose a message between the two steps. If a worker dies before acking,
the scheduler moves expired in-flight entries back to the queue (the same
"visibility timeout" idea as AWS SQS).

Priority is strict: low-priority jobs only run when higher lists are empty.
"""
from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

import redis.asyncio as redis

from .base import Broker, Delivery

PRIORITIES = ("high", "normal", "low")
QUEUE = "tq:queue:{}"
INFLIGHT = "tq:inflight"
PRIORITY_OF = "tq:priority"  # HASH job_id -> priority, so expired messages go back to the right list

# KEYS = queue lists in priority order + inflight zset; ARGV[1] = visibility deadline
POP_LUA = """
for i = 1, #KEYS - 1 do
  local id = redis.call('RPOP', KEYS[i])
  if id then
    redis.call('ZADD', KEYS[#KEYS], ARGV[1], id)
    return id
  end
end
return nil
"""

# Move expired in-flight messages back to the *front* of their queue (they've waited long enough).
REQUEUE_LUA = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 500)
for _, id in ipairs(ids) do
  redis.call('ZREM', KEYS[1], id)
  local p = redis.call('HGET', KEYS[2], id) or 'normal'
  redis.call('RPUSH', 'tq:queue:' .. p, id)
end
return #ids
"""


class RedisBroker(Broker):
    name = "redis"

    def __init__(self, url: str, visibility_s: float = 60.0, poll_interval_s: float = 0.2):
        self._url = url
        self._visibility = visibility_s
        self._poll = poll_interval_s
        self.r: redis.Redis | None = None

    async def connect(self):
        self.r = redis.from_url(self._url, decode_responses=True)
        await self.r.ping()
        self._pop = self.r.register_script(POP_LUA)
        self._requeue = self.r.register_script(REQUEUE_LUA)

    async def close(self):
        if self.r:
            await self.r.aclose()

    async def publish(self, job_id: str, priority: str) -> None:
        priority = priority if priority in PRIORITIES else "normal"
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.hset(PRIORITY_OF, job_id, priority)
            pipe.lpush(QUEUE.format(priority), job_id)
            await pipe.execute()

    async def _ack(self, job_id: str) -> None:
        async with self.r.pipeline(transaction=True) as pipe:
            pipe.zrem(INFLIGHT, job_id)
            pipe.hdel(PRIORITY_OF, job_id)
            await pipe.execute()

    async def consume(self, prefetch: int) -> AsyncIterator[Delivery]:
        keys = [QUEUE.format(p) for p in PRIORITIES] + [INFLIGHT]
        while True:
            job_id = await self._pop(keys=keys, args=[time.time() + self._visibility])
            if job_id is None:
                # Polling keeps the Lua pop atomic across 3 lists; BLMOVE only works on a single list.
                await asyncio.sleep(self._poll)
                continue
            yield Delivery(job_id=job_id, ack=lambda jid=job_id: self._ack(jid))

    async def depth(self) -> dict[str, int]:
        async with self.r.pipeline(transaction=False) as pipe:
            for p in PRIORITIES:
                pipe.llen(QUEUE.format(p))
            pipe.zcard(INFLIGHT)
            *lens, inflight = await pipe.execute()
        return {**dict(zip(PRIORITIES, lens)), "inflight": inflight}

    async def requeue_expired(self) -> int:
        return int(await self._requeue(keys=[INFLIGHT, PRIORITY_OF], args=[time.time()]))
