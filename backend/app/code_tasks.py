"""Validation for code tasks (Python script / shell command) and their input variables.

A code task's payload::

    {"language": "python" | "shell",
     "code": "...",
     "variables": [{"name": "city", "type": "string", "default": "Pune",
                    "required": true, "description": "City to report on"}],
     "inputs": {"city": "Mumbai"}}          # supplied per run

Shared by the API (fail fast with 422) and the worker handler (defence in depth).
"""
from __future__ import annotations

import re

LANGUAGES = ("python", "shell")
VAR_TYPES = ("string", "number", "boolean")
# Lowercase only: can never collide with PATH, HOME, LD_PRELOAD, PYTHONPATH... in the shell environment.
VAR_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,31}$")
MAX_CODE_CHARS = 64_000
MAX_VARIABLES = 20
MAX_VALUE_CHARS = 10_000
PAYLOAD_KEYS = {"language", "code", "variables", "inputs"}


def coerce(var_type: str, value):
    """Convert a raw value (often a string from a form) to the variable's type."""
    if var_type == "string":
        value = str(value)
        if len(value) > MAX_VALUE_CHARS:
            raise ValueError(f"value longer than {MAX_VALUE_CHARS} characters")
        return value
    if var_type == "number":
        if isinstance(value, bool):
            raise ValueError("expected a number")
        if isinstance(value, (int, float)):
            return value
        text = str(value).strip()
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                raise ValueError(f"expected a number, got {value!r}") from None
    if var_type == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"expected true/false, got {value!r}")
    raise ValueError(f"unknown type {var_type!r}")


def _missing(var_type: str, value) -> bool:
    return value is None or (value == "" and var_type != "string")


def validate_variables(variables) -> list[dict]:
    if not isinstance(variables, list):
        raise ValueError("variables must be a list")
    if len(variables) > MAX_VARIABLES:
        raise ValueError(f"at most {MAX_VARIABLES} variables")
    seen, clean = set(), []
    for i, v in enumerate(variables):
        if not isinstance(v, dict):
            raise ValueError(f"variables[{i}] must be an object")
        name = v.get("name", "")
        if not isinstance(name, str) or not VAR_NAME.match(name):
            raise ValueError(f"variable name {name!r} is invalid: use lowercase letters, digits and _ "
                             "(must not start with a digit)")
        if name in seen:
            raise ValueError(f"duplicate variable {name!r}")
        seen.add(name)
        vtype = v.get("type", "string")
        if vtype not in VAR_TYPES:
            raise ValueError(f"variable {name!r}: type must be one of {VAR_TYPES}")
        default = v.get("default")
        if not _missing(vtype, default):
            try:
                default = coerce(vtype, default)
            except ValueError as exc:
                raise ValueError(f"variable {name!r} default: {exc}") from None
        else:
            default = None
        clean.append({"name": name, "type": vtype, "default": default,
                      "required": bool(v.get("required", False)),
                      "description": str(v.get("description", ""))[:200]})
    return clean


def validate_code_payload(payload: dict, recurring: bool = False) -> None:
    extra = set(payload) - PAYLOAD_KEYS
    if extra:
        raise ValueError(f"unexpected payload keys for a code task: {sorted(extra)}")
    if payload.get("language") not in LANGUAGES:
        raise ValueError(f"language must be one of {LANGUAGES}")
    code = payload.get("code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must not be empty")
    if len(code) > MAX_CODE_CHARS:
        raise ValueError(f"code is longer than {MAX_CODE_CHARS} characters")
    variables = validate_variables(payload.get("variables", []))
    if recurring:
        missing = [v["name"] for v in variables if v["required"] and v["default"] is None]
        if missing:
            raise ValueError(f"recurring tasks run without user input: give defaults to required variables {missing}")
    if "inputs" in payload:
        resolve_inputs(variables, payload["inputs"])


def resolve_inputs(variables: list[dict], given) -> dict:
    """Apply defaults, check required variables and coerce types.

    With no declared variables (ad-hoc jobs) any valid names with scalar values are accepted.
    """
    given = given or {}
    if not isinstance(given, dict):
        raise ValueError("inputs must be an object")
    if not variables:
        out = {}
        for k, v in given.items():
            if not isinstance(k, str) or not VAR_NAME.match(k):
                raise ValueError(f"input name {k!r} is invalid")
            if not isinstance(v, (str, int, float, bool)) and v is not None:
                raise ValueError(f"input {k!r} must be a string, number or boolean")
            out[k] = coerce("string", v) if isinstance(v, str) else v
        return out

    declared = {v["name"] for v in variables}
    unknown = set(given) - declared
    if unknown:
        raise ValueError(f"unknown inputs {sorted(unknown)}; declared variables: {sorted(declared)}")
    resolved = {}
    for v in variables:
        raw = given.get(v["name"])
        if _missing(v["type"], raw):
            raw = v.get("default")
        if _missing(v["type"], raw):
            if v.get("required"):
                raise ValueError(f"missing required input {v['name']!r}")
            resolved[v["name"]] = None
            continue
        try:
            resolved[v["name"]] = coerce(v["type"], raw)
        except ValueError as exc:
            raise ValueError(f"input {v['name']!r}: {exc}") from None
    return resolved
