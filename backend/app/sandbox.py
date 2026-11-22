"""Execute user-supplied Python / shell code in a constrained subprocess.

This module runs ONLY inside the isolated ``runner`` container (see runner.py
and docker-compose.yml). Defence in depth:

Container level (docker-compose)          Process level (this file)
--------------------------------          --------------------------------------------
* no secrets in its environment           * clean env: PATH/HOME/LANG + inputs only
* internal-only network: can't reach      * fresh temp working directory per run
  Postgres, Redis or the internet         * rlimits: CPU seconds, address space,
* read-only root fs, tmpfs /tmp             file size, open files, no core dumps
* non-root user, all capabilities         * own process group, SIGKILL'ed on timeout
  dropped, no-new-privileges                and after exit (kills background children)
* pids_limit (fork bombs), mem/cpu caps   * stdout/stderr capped (rest discarded)

Inputs are passed as environment variables and as a JSON ``inputs`` dict - never
spliced into the code text - so input values cannot inject shell commands.
"""
from __future__ import annotations

import asyncio
import json
import os
import resource
import signal
import tempfile
import time
from functools import partial

LANGUAGES = ("python", "shell")
RESULT_FILE = "result.json"
MAX_RESULT_BYTES = 1_000_000

# Runs user code with `inputs` predefined; a top-level `result` variable becomes structured output.
# runpy keeps the user's own file name and line numbers in tracebacks.
PY_WRAPPER = """import json, os, runpy, sys
inputs = json.loads(os.environ.get("TASK_INPUTS_JSON", "{}"))
ns = runpy.run_path("main.py", init_globals={"inputs": inputs}, run_name="__main__")
if ns.get("result") is not None:
    try:
        with open("result.json", "w") as fh:
            json.dump(ns["result"], fh, default=str)
    except Exception as exc:
        print(f"[sandbox] could not serialise `result`: {exc}", file=sys.stderr)
"""


def _apply_limits(cpu_s: int, memory_mb: int, max_file_mb: int) -> None:  # runs in the child before exec
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 2**20, memory_mb * 2**20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_mb * 2**20, max_file_mb * 2**20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class _Capture:
    """Keeps the first `limit` bytes of a stream and keeps draining the rest (so the child never blocks)."""

    def __init__(self, limit: int):
        self.limit, self.buf, self.total = limit, bytearray(), 0

    async def drain(self, stream: asyncio.StreamReader) -> None:
        while chunk := await stream.read(65536):
            self.total += len(chunk)
            if len(self.buf) < self.limit:
                self.buf += chunk[: self.limit - len(self.buf)]

    def text(self) -> str:
        return self.buf.decode(errors="replace")

    @property
    def truncated(self) -> bool:
        return self.total > self.limit


def _env_value(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def execute(language: str, code: str, inputs: dict, timeout_s: float, *,
                  memory_mb: int = 256, max_output: int = 64_000, max_file_mb: int = 16) -> dict:
    if language not in LANGUAGES:
        raise ValueError(f"language must be one of {LANGUAGES}")
    with tempfile.TemporaryDirectory(prefix="run-") as wd:
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": wd, "TMPDIR": wd, "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "TASK_INPUTS_JSON": json.dumps(inputs),
        }
        # Variable names are validated as lowercase identifiers, so they can't clobber PATH, LD_PRELOAD, etc.
        env.update({k: _env_value(v) for k, v in inputs.items() if v is not None})

        if language == "python":
            with open(os.path.join(wd, "main.py"), "w") as fh:
                fh.write(code)
            with open(os.path.join(wd, "_wrapper.py"), "w") as fh:
                fh.write(PY_WRAPPER)
            argv = ["python3", "-I", "_wrapper.py"]  # -I: isolated mode, ignores PYTHON* env and user site
        else:
            with open(os.path.join(wd, "main.sh"), "w") as fh:
                fh.write(code)
            argv = ["/bin/bash", "--noprofile", "--norc", "main.sh"]

        out, err = _Capture(max_output), _Capture(max_output)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=wd, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group => we can kill the whole tree
            preexec_fn=partial(_apply_limits, int(timeout_s) + 1, memory_mb, max_file_mb),
        )
        readers = [asyncio.create_task(out.drain(proc.stdout)), asyncio.create_task(err.drain(proc.stderr))]
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
        _kill_group(proc.pid)  # timeout, or leftover background processes after a normal exit
        await proc.wait()
        # A child that escaped the group could keep the pipes open: don't wait forever for EOF.
        done, pending = await asyncio.wait(readers, timeout=2)
        for t in pending:
            t.cancel()
        duration_ms = int((time.monotonic() - started) * 1000)

        result = None
        result_path = os.path.join(wd, RESULT_FILE)
        if os.path.exists(result_path) and os.path.getsize(result_path) <= MAX_RESULT_BYTES:
            try:
                with open(result_path) as fh:
                    result = json.load(fh)
            except ValueError:
                pass

    return {
        "language": language,
        "exit_code": None if timed_out else proc.returncode,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": out.text(), "stdout_truncated": out.truncated,
        "stderr": err.text(), "stderr_truncated": err.truncated,
        "result": result,
    }
