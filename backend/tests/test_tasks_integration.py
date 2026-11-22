"""End-to-end tests for user-created tasks (needs the docker compose stack).

    API_URL=http://localhost:8080 pytest -m integration
"""
import time
import uuid

import pytest

from tests.test_integration import _cleanup_created_tasks, client, wait_for  # noqa: F401

pytestmark = pytest.mark.integration


def name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def test_create_run_and_history(client):
    n = name("fib")
    r = client.post("/api/tasks", json={"name": n, "description": "fibonacci", "handler": "fibonacci",
                                        "default_payload": {"n": 10}, "priority": "high", "max_retries": 1})
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["interval_s"] is None and task["next_run_at"] is None

    assert client.post("/api/tasks", json={"name": n, "handler": "sleep"}).status_code == 409

    job = client.post(f"/api/tasks/{n}/run").json()
    assert job["task_name"] == n and job["type"] == "fibonacci" and job["priority"] == "high"
    assert wait_for(client, job["id"], {"succeeded"})["result"]["value"] == "55"

    # Payload override is merged over the defaults.
    job2 = client.post(f"/api/tasks/{n}/run", json={"payload": {"n": 20}}).json()
    assert wait_for(client, job2["id"], {"succeeded"})["result"]["value"] == "6765"

    runs = client.get("/api/jobs", params={"task": n}).json()
    assert runs["total"] == 2
    listed = next(t for t in client.get("/api/tasks").json() if t["name"] == n)
    assert listed["run_count"] == 2 and listed["stats"]["succeeded"] == 2


def test_update_and_delete(client):
    n = name("upd")
    client.post("/api/tasks", json={"name": n, "handler": "sleep", "default_payload": {"seconds": 0}})
    r = client.patch(f"/api/tasks/{n}", json={"description": "changed", "max_retries": 5, "priority": None})
    assert r.status_code == 200
    assert r.json()["description"] == "changed" and r.json()["max_retries"] == 5
    assert r.json()["priority"] == "normal"  # null means "unchanged"
    assert client.patch(f"/api/tasks/{n}", json={"interval_seconds": 1}).status_code == 422
    assert client.delete(f"/api/tasks/{n}").status_code == 204
    assert client.get(f"/api/tasks/{n}").status_code == 404
    assert client.post(f"/api/tasks/{n}/run").status_code == 404


def test_recurring_task_fires_and_can_be_paused(client):
    n = name("tick")
    r = client.post("/api/tasks", json={"name": n, "handler": "sleep", "default_payload": {"seconds": 0},
                                        "interval_seconds": 5})
    assert r.json()["next_run_at"] is not None
    deadline = time.time() + 20
    while time.time() < deadline:
        if client.get("/api/jobs", params={"task": n}).json()["total"] >= 2:
            break
        time.sleep(1)
    assert client.get("/api/jobs", params={"task": n}).json()["total"] >= 2

    paused = client.patch(f"/api/tasks/{n}", json={"enabled": False}).json()
    assert paused["next_run_at"] is None
    before = client.get("/api/jobs", params={"task": n}).json()["total"]
    time.sleep(7)
    assert client.get("/api/jobs", params={"task": n}).json()["total"] == before
    client.delete(f"/api/tasks/{n}")


def test_webhook_task(client):
    n = name("hook")
    r = client.post("/api/tasks", json={
        "name": n, "handler": "http_request",
        "default_payload": {"method": "GET", "url": "http://api:8000/api/health", "expected_status": [200]}})
    assert r.status_code == 201, r.text
    job = wait_for(client, client.post(f"/api/tasks/{n}/run").json()["id"], {"succeeded"})
    assert job["result"]["status_code"] == 200 and job["result"]["body"]["status"] == "ok"

    # 404 is a client error: fails permanently, no retries wasted.
    job = client.post(f"/api/tasks/{n}/run", json={"payload": {"url": "http://api:8000/nope"}}).json()
    job = wait_for(client, job["id"], {"dead"})
    assert job["attempts"] == 1 and "404" in job["error"]


def test_ssrf_blocked_task(client):
    n = name("ssrf")
    client.post("/api/tasks", json={"name": n, "handler": "http_request", "max_retries": 3,
                                    "default_payload": {"url": "http://169.254.169.254/latest/meta-data/"}})
    job = wait_for(client, client.post(f"/api/tasks/{n}/run").json()["id"], {"dead"})
    assert job["attempts"] == 1 and "SSRF" in job["error"]


def test_task_validation(client):
    assert client.post("/api/tasks", json={"name": "Bad Name!", "handler": "sleep"}).status_code == 422
    assert client.post("/api/tasks", json={"name": name("x"), "handler": "nope"}).status_code == 422
    assert client.post("/api/tasks", json={"name": name("x"), "handler": "http_request",
                                           "default_payload": {"url": "ftp://x"}}).status_code == 422
