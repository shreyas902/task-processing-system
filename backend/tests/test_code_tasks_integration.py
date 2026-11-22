"""End-to-end tests for Python / shell code tasks (needs the docker compose stack with the runner)."""
import uuid

import pytest

from tests.test_integration import _cleanup_created_tasks, client, wait_for  # noqa: F401

pytestmark = pytest.mark.integration


def name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def create(client, **task):
    r = client.post("/api/tasks", json=task)
    assert r.status_code == 201, r.text
    return r.json()


def test_python_task_with_variables(client):
    n = name("py")
    task = create(client, name=n, description="Greets someone and doubles a number", handler="run_code",
                  max_retries=0, default_payload={
                      "language": "python",
                      "code": "print(f\"Hello {inputs['who']}\")\nresult = {'double': inputs['n'] * 2, 'loud': inputs['loud']}",
                      "variables": [
                          {"name": "who", "type": "string", "required": True, "description": "Name"},
                          {"name": "n", "type": "number", "default": "5"},
                          {"name": "loud", "type": "boolean", "default": False},
                      ]})
    assert task["default_payload"]["variables"][1]["default"] == 5  # normalised on save

    job = client.post(f"/api/tasks/{n}/run", json={"payload": {"inputs": {"who": "Shreyas", "loud": "true"}}}).json()
    job = wait_for(client, job["id"], {"succeeded", "dead"})
    assert job["status"] == "succeeded", job["error"]
    assert job["result"]["stdout"] == "Hello Shreyas\n"
    assert job["result"]["result"] == {"double": 10, "loud": True}
    assert job["payload"]["inputs"] == {"who": "Shreyas", "loud": "true"}

    # Validation happens before a job is created.
    assert client.post(f"/api/tasks/{n}/run", json={"payload": {"inputs": {}}}).status_code == 422          # required
    assert client.post(f"/api/tasks/{n}/run", json={"payload": {"inputs": {"who": "x", "n": "lots"}}}).status_code == 422
    assert client.post(f"/api/tasks/{n}/run", json={"payload": {"code": "print('hijack')"}}).status_code == 422


def test_shell_task(client):
    n = name("sh")
    create(client, name=n, handler="run_code", default_payload={
        "language": "shell",
        "code": 'echo "files for $prefix:"; for i in $(seq 1 "$count"); do echo "$prefix-$i"; done',
        "variables": [{"name": "prefix", "default": "report"}, {"name": "count", "type": "number", "default": 3}]})
    job = wait_for(client, client.post(f"/api/tasks/{n}/run").json()["id"], {"succeeded", "dead"})
    assert job["result"]["stdout"] == "files for report:\nreport-1\nreport-2\nreport-3\n"


def test_failing_script_reports_stderr(client):
    n = name("fail")
    create(client, name=n, handler="run_code", max_retries=1,
           default_payload={"language": "python", "code": "import sys\nprint('working')\nsys.exit('disk full')"})
    job = wait_for(client, client.post(f"/api/tasks/{n}/run").json()["id"], {"dead"}, timeout=60)
    assert job["attempts"] == 2
    assert "exited with code 1" in job["error"] and "disk full" in job["error"] and "working" in job["error"]


def test_script_timeout(client):
    n = name("slow")
    create(client, name=n, handler="run_code", max_retries=0, timeout_seconds=3,
           default_payload={"language": "shell", "code": "echo started; sleep 60"})
    job = wait_for(client, client.post(f"/api/tasks/{n}/run").json()["id"], {"dead"})
    assert "timed out" in job["error"] and "started" in job["error"]


def test_sandbox_isolation(client):
    """User code can't see secrets or reach the database / broker / internet."""
    n = name("probe")
    code = (
        "import os, socket\n"
        "leaks = [k for k in os.environ if any(s in k for s in ('DATABASE', 'REDIS', 'RABBIT', 'TOKEN'))]\n"
        "reach = {}\n"
        "for host, port in [('postgres', 5432), ('redis', 6379), ('api', 8000), ('1.1.1.1', 443)]:\n"
        "    try:\n"
        "        socket.create_connection((host, port), timeout=2); reach[host] = True\n"
        "    except OSError:\n"
        "        reach[host] = False\n"
        "result = {'leaks': leaks, 'reach': reach, 'uid': os.getuid()}\n")
    create(client, name=n, handler="run_code", max_retries=0, default_payload={"language": "python", "code": code})
    job = wait_for(client, client.post(f"/api/tasks/{n}/run").json()["id"], {"succeeded", "dead"})
    assert job["status"] == "succeeded", job["error"]
    res = job["result"]["result"]
    assert res["leaks"] == [] and res["uid"] != 0
    assert not any(res["reach"].values()), res["reach"]


def test_code_task_validation(client):
    bad = [
        {"language": "ruby", "code": "puts 1"},
        {"language": "python", "code": ""},
        {"language": "shell", "code": "echo $PATH", "variables": [{"name": "PATH"}]},
    ]
    for payload in bad:
        r = client.post("/api/tasks", json={"name": name("bad"), "handler": "run_code", "default_payload": payload})
        assert r.status_code == 422, payload
    # A recurring task can't prompt for input: required variables need defaults.
    r = client.post("/api/tasks", json={"name": name("cron"), "handler": "run_code", "interval_seconds": 60,
                                        "default_payload": {"language": "shell", "code": "echo $x",
                                                            "variables": [{"name": "x", "required": True}]}})
    assert r.status_code == 422 and "recurring" in r.text
