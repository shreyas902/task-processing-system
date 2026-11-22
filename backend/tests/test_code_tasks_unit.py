import asyncio
import time

import pytest

from app.code_tasks import coerce, resolve_inputs, validate_code_payload, validate_variables
from app.sandbox import execute


def run(language, code, inputs=None, timeout=10, **kw):
    return asyncio.run(execute(language, code, inputs or {}, timeout, **kw))


# ----------------------------------------------------------------- validation
def test_variables_are_normalised():
    v = validate_variables([{"name": "count", "type": "number", "default": "3", "required": True}])
    assert v == [{"name": "count", "type": "number", "default": 3, "required": True, "description": ""}]


@pytest.mark.parametrize("name", ["PATH", "Home", "1x", "ld-preload", "a" * 40, ""])
def test_variable_names_must_be_lowercase_identifiers(name):
    with pytest.raises(ValueError, match="invalid"):
        validate_variables([{"name": name}])


def test_duplicate_and_bad_types():
    with pytest.raises(ValueError, match="duplicate"):
        validate_variables([{"name": "a"}, {"name": "a"}])
    with pytest.raises(ValueError, match="type"):
        validate_variables([{"name": "a", "type": "list"}])


@pytest.mark.parametrize("t,raw,expected", [
    ("number", "42", 42), ("number", "2.5", 2.5), ("boolean", "yes", True),
    ("boolean", "0", False), ("string", 7, "7"),
])
def test_coerce(t, raw, expected):
    assert coerce(t, raw) == expected


def test_resolve_inputs_defaults_required_and_unknown():
    variables = validate_variables([
        {"name": "city", "type": "string", "required": True},
        {"name": "days", "type": "number", "default": 3},
        {"name": "verbose", "type": "boolean"},
    ])
    assert resolve_inputs(variables, {"city": "Pune"}) == {"city": "Pune", "days": 3, "verbose": None}
    assert resolve_inputs(variables, {"city": "Pune", "days": "7", "verbose": "true"})["days"] == 7
    with pytest.raises(ValueError, match="missing required input 'city'"):
        resolve_inputs(variables, {})
    with pytest.raises(ValueError, match="unknown inputs"):
        resolve_inputs(variables, {"city": "x", "evil": "1"})
    with pytest.raises(ValueError, match="expected a number"):
        resolve_inputs(variables, {"city": "x", "days": "many"})


def test_recurring_code_task_needs_defaults_for_required_vars():
    payload = {"language": "shell", "code": "echo $x", "variables": [{"name": "x", "required": True}]}
    validate_code_payload(payload)  # fine when run on demand
    with pytest.raises(ValueError, match="recurring"):
        validate_code_payload(payload, recurring=True)


def test_code_payload_rejects_unknown_keys_and_empty_code():
    with pytest.raises(ValueError, match="unexpected"):
        validate_code_payload({"language": "python", "code": "print(1)", "cmd": "x"})
    with pytest.raises(ValueError, match="empty"):
        validate_code_payload({"language": "python", "code": "  "})


# -------------------------------------------------------------------- sandbox
def test_python_inputs_stdout_and_result():
    r = run("python", "print('hi', inputs['name'])\nresult = {'n': inputs['n'] * 2}", {"name": "Shreyas", "n": 21})
    assert r["exit_code"] == 0 and r["stdout"] == "hi Shreyas\n" and r["result"] == {"n": 42}


def test_python_exception_gives_traceback_with_user_line_numbers():
    r = run("python", "x = 1\nraise ValueError('boom')")
    assert r["exit_code"] == 1
    assert 'main.py", line 2' in r["stderr"] and "ValueError: boom" in r["stderr"]


def test_shell_inputs_are_env_vars_not_code():
    # A malicious-looking value is just data: it is never spliced into the script.
    r = run("shell", 'echo "hello $name"; echo "flag=$flag"', {"name": "x; touch /tmp/pwned; echo", "flag": True})
    assert r["exit_code"] == 0
    assert r["stdout"] == "hello x; touch /tmp/pwned; echo\nflag=true\n"


def test_shell_exit_code_and_stderr():
    r = run("shell", "echo oops >&2; exit 3")
    assert r["exit_code"] == 3 and r["stderr"] == "oops\n"


def test_environment_is_clean(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    r = run("shell", "env")
    assert "secret" not in r["stdout"] and "DATABASE_URL" not in r["stdout"]


def test_timeout_kills_process_tree():
    t = time.monotonic()
    r = run("shell", "sleep 30 & sleep 30", timeout=1)
    assert r["timed_out"] and r["exit_code"] is None
    assert time.monotonic() - t < 6


def test_output_is_capped():
    r = run("python", "print('x' * 1_000_000)", max_output=1000)
    assert len(r["stdout"]) == 1000 and r["stdout_truncated"]


def test_memory_limit():
    r = run("python", "data = bytearray(400 * 1024 * 1024)\nprint('allocated')", memory_mb=200)
    assert r["exit_code"] != 0 and "MemoryError" in r["stderr"]
