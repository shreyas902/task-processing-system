import asyncio
import random

import pytest

from app.handlers import REGISTRY, PermanentError, run_handler
from app.state import AMQP_PRIORITY, Priority, backoff_delay, should_retry


def test_backoff_is_exponential_and_capped():
    rng = random.Random(0)
    no_jitter = [backoff_delay(a, base=2, cap=60, jitter=0, rng=rng) for a in range(1, 8)]
    assert no_jitter == [2, 4, 8, 16, 32, 60, 60]


def test_backoff_jitter_stays_in_bounds():
    rng = random.Random(1)
    for _ in range(200):
        d = backoff_delay(3, base=2, cap=60, jitter=0.2, rng=rng)
        assert 6.4 <= d <= 9.6


@pytest.mark.parametrize("attempts,max_retries,expected", [(1, 3, True), (3, 3, True), (4, 3, False), (1, 0, False)])
def test_should_retry(attempts, max_retries, expected):
    assert should_retry(attempts, max_retries) is expected


def test_amqp_priority_accepts_plain_strings():
    assert AMQP_PRIORITY["high"] > AMQP_PRIORITY["normal"] > AMQP_PRIORITY[Priority.LOW]


def test_handlers():
    assert asyncio.run(run_handler("fibonacci", {"n": 10}))["value"] == "55"
    assert asyncio.run(run_handler("word_count", {"text": "a b a"}))["top"][0] == ("a", 2)
    with pytest.raises(PermanentError):
        asyncio.run(run_handler("nope", {}))
    with pytest.raises(PermanentError):
        asyncio.run(run_handler("fibonacci", {"n": -1}))


def test_registry_has_examples():
    for name, entry in REGISTRY.items():
        assert entry["description"] and isinstance(entry["example"], dict), name
