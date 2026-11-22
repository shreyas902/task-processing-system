from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://tasks:tasks@localhost:5432/tasks"

    # Message broker used to hand job ids to workers: "redis" or "rabbitmq".
    broker: str = "redis"
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"

    # Worker
    worker_concurrency: int = 4
    worker_heartbeat_s: float = 5.0
    shutdown_grace_s: float = 30.0

    # Retry policy: delay = base * 2^(attempt-1), capped, with +/-20% jitter.
    retry_base_delay_s: float = 2.0
    retry_max_delay_s: float = 300.0

    # Scheduler / reaper
    scheduler_interval_s: float = 1.0
    lease_grace_s: float = 30.0           # extra time after job timeout before a RUNNING job is presumed dead
    claim_visibility_s: float = 60.0      # Redis: popped-but-unclaimed message is redelivered after this
    stale_queued_s: float = 300.0         # QUEUED with no claim for this long => republish (lost publish)

    # HTTP tasks: hosts allowed even though they resolve to private IPs (comma separated).
    http_allowed_hosts: str = ""
    # Code tasks (Python / shell) run in the isolated sandbox runner service.
    runner_url: str = "http://localhost:9000"
    runner_token: str = "dev-runner-token"
    runner_max_concurrency: int = 8
    runner_memory_mb: int = 256       # address-space limit per script
    runner_max_output: int = 64_000   # bytes kept from stdout / stderr each

    # Recurring tasks can't run more often than this.
    min_task_interval_s: int = 5

    cors_origins: str = "*"


@lru_cache
def get_settings() -> Settings:
    return Settings()
