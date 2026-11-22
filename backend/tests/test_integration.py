"""End-to-end tests against a running stack (docker compose up).

    API_URL=http://localhost:8080 pytest -m integration
"""
import os
import time

import httpx
import pytest

API = os.environ.get("API_URL", "http://localhost:8080")
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    c = httpx.Client(base_url=API, timeout=10)
    try:
        c.get("/api/health").raise_for_status()
    except Exception:
        pytest.skip(f"stack not running at {API}")
    yield c
    c.close()


def wait_for(client, job_id, statuses, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} stuck in {job['status']}")


def test_job_succeeds(client):
    r = client.post("/api/jobs", json={"type": "fibonacci", "payload": {"n": 50}})
    assert r.status_code == 201
    job = wait_for(client, r.json()["id"], {"succeeded"})
    assert job["result"]["value"] == "12586269025"
    assert [e["event"] for e in job["events"]] == ["created", "started", "succeeded"]


def test_retries_then_dead_letter(client):
    r = client.post("/api/jobs", json={"type": "always_fail", "max_retries": 2})
    job = wait_for(client, r.json()["id"], {"dead"}, timeout=90)
    assert job["attempts"] == 3
    events = [e["event"] for e in job["events"]]
    assert events.count("retry_scheduled") == 2 and events[-1] == "dead_lettered"
    dlq = client.get("/api/dead-letter").json()
    assert any(j["id"] == job["id"] for j in dlq["items"])
    # Manual retry from the DLQ puts it back to work (and it fails again).
    assert client.post(f"/api/jobs/{job['id']}/retry").json()["status"] == "queued"
    wait_for(client, job["id"], {"dead"}, timeout=90)


def test_permanent_error_skips_retries(client):
    r = client.post("/api/jobs", json={"type": "http_check", "payload": {"url": "ftp://x"}, "max_retries": 5})
    job = wait_for(client, r.json()["id"], {"dead"})
    assert job["attempts"] == 1


def test_timeout(client):
    r = client.post("/api/jobs", json={"type": "sleep", "payload": {"seconds": 5},
                                       "timeout_seconds": 1, "max_retries": 0})
    job = wait_for(client, r.json()["id"], {"dead"})
    assert "Timed out" in job["error"]


def test_delayed_job_and_cancel(client):
    r = client.post("/api/jobs", json={"type": "sleep", "payload": {"seconds": 0}, "delay_seconds": 2})
    job = r.json()
    assert job["status"] == "scheduled"
    wait_for(client, job["id"], {"succeeded"})

    r = client.post("/api/jobs", json={"type": "sleep", "delay_seconds": 600})
    jid = r.json()["id"]
    assert client.post(f"/api/jobs/{jid}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 409


def test_idempotency_key(client):
    key = f"order-{time.time()}"
    a = client.post("/api/jobs", json={"type": "sleep", "payload": {"seconds": 0}, "idempotency_key": key})
    b = client.post("/api/jobs", json={"type": "sleep", "payload": {"seconds": 0}, "idempotency_key": key})
    assert a.status_code == 201 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


def test_validation(client):
    assert client.post("/api/jobs", json={"type": "rm -rf"}).status_code == 422
    assert client.post("/api/jobs", json={"type": "sleep", "priority": "urgent"}).status_code == 422


def test_bulk_and_stats(client):
    r = client.post("/api/jobs/bulk", json={"type": "sleep", "payload": {"seconds": 0.2}, "count": 20})
    ids = r.json()["ids"]
    for jid in ids:
        wait_for(client, jid, {"succeeded"})
    stats = client.get("/api/stats").json()
    assert stats["by_status"]["succeeded"] >= 20
    workers = client.get("/api/workers").json()
    assert any(w["alive"] for w in workers)


@pytest.fixture(autouse=True, scope="module")
def _cleanup_created_tasks(client):
    """Delete task definitions created by a test module so test runs don't pile up in the UI."""
    before = {t["name"] for t in client.get("/api/tasks").json()}
    yield
    for t in client.get("/api/tasks").json():
        if t["name"] not in before:
            client.delete(f"/api/tasks/{t['name']}")
