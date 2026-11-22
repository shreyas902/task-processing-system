"""RabbitMQ broker (AMQP 0-9-1 via aio-pika).

* One durable queue ``tq.jobs`` declared with ``x-max-priority=10`` - RabbitMQ
  orders messages by priority natively.
* Messages are persistent (``delivery_mode=2``) so they survive a broker restart.
* ``prefetch_count`` (QoS) limits un-acked messages per worker = its concurrency,
  giving natural back-pressure and fair dispatch between workers.
* Manual acks: if a worker's connection drops before ack, RabbitMQ redelivers
  the message automatically - no reaper needed.
"""
from __future__ import annotations

from typing import AsyncIterator

import aio_pika

from app.state import AMQP_PRIORITY, Priority

from .base import Broker, Delivery

QUEUE_NAME = "tq.jobs"


class RabbitMQBroker(Broker):
    name = "rabbitmq"

    def __init__(self, url: str):
        self._url = url
        self._conn: aio_pika.abc.AbstractRobustConnection | None = None

    async def connect(self):
        self._conn = await aio_pika.connect_robust(self._url)
        self._channel = await self._conn.channel(publisher_confirms=True)
        self._queue = await self._declare(self._channel)

    @staticmethod
    async def _declare(channel):
        return await channel.declare_queue(QUEUE_NAME, durable=True, arguments={"x-max-priority": 10})

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def publish(self, job_id: str, priority: str) -> None:
        prio = AMQP_PRIORITY.get(priority, AMQP_PRIORITY[Priority.NORMAL])  # str-Enum keys match plain strings
        await self._channel.default_exchange.publish(
            aio_pika.Message(body=job_id.encode(), priority=prio,
                             delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
            routing_key=QUEUE_NAME,
        )

    async def consume(self, prefetch: int) -> AsyncIterator[Delivery]:
        channel = await self._conn.channel()
        await channel.set_qos(prefetch_count=prefetch)
        queue = await self._declare(channel)
        async with queue.iterator() as it:
            async for message in it:
                yield Delivery(job_id=message.body.decode(), ack=message.ack)

    async def depth(self) -> dict[str, int]:
        q = await self._channel.declare_queue(QUEUE_NAME, passive=True)
        return {"ready": q.declaration_result.message_count,
                "consumers": q.declaration_result.consumer_count}
