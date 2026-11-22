from app.config import Settings

from .base import Broker, Delivery


def create_broker(settings: Settings) -> Broker:
    if settings.broker == "rabbitmq":
        from .rabbitmq_broker import RabbitMQBroker
        return RabbitMQBroker(settings.rabbitmq_url)
    if settings.broker == "redis":
        from .redis_broker import RedisBroker
        return RedisBroker(settings.redis_url, visibility_s=settings.claim_visibility_s)
    raise ValueError(f"Unknown broker: {settings.broker!r} (expected 'redis' or 'rabbitmq')")


__all__ = ["Broker", "Delivery", "create_broker"]
