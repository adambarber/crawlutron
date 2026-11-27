"""Shared primitives for the automated pull-request review contract."""

from __future__ import annotations

import json as _json
import re as _re
from pathlib import Path as _Path
from typing import Final, TypeGuard

import httpx as _httpx

from tests._support._review_contract import (
    ProbeResultRecord,
    probe_argv,
    probe_duration_ms,
    probe_exit_code,
    probe_name,
    probe_stderr_path,
    probe_stderr_sha256,
    probe_stdout_path,
    probe_stdout_sha256,
)

__all__ = [
    "GITHUB_API_URL",
    "REPO_ROOT",
    "ProbeResultRecord",
    "as_mapping",
    "as_probe_result_record",
    "error_text",
    "http_get",
    "http_get_text",
    "http_post",
    "load_json_object",
    "non_empty_string",
    "probe_argv",
    "probe_duration_ms",
    "probe_exit_code",
    "probe_name",
    "probe_stderr_path",
    "probe_stderr_sha256",
    "probe_stdout_path",
    "probe_stdout_sha256",
    "validate_probe_artifact",
]

REPO_ROOT: Final[_Path] = _Path(__file__).resolve().parents[1]
GITHUB_API_URL: Final[str] = "https://api.github.com"
_SHA256_PATTERN: Final[_re.Pattern[str]] = _re.compile(r"[0-9a-f]{64}")

_PROBE_RESULT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "argv",
        "exit_code",
        "duration_ms",
        "stdout_path",
        "stderr_path",
        "stdout_sha256",
        "stderr_sha256",
    }
)


def _is_str(value: object) -> TypeGuard[str]:
    return isinstance(value, str)


def _is_int_not_bool(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonneg_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_list_str(value: object) -> TypeGuard[list[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_sha256(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _validate_probe_result_fields(result: dict[str, object], label: str) -> None:
    """Validate the eight fields of a probe result record."""
    if set(result) != _PROBE_RESULT_FIELDS:
        raise ValueError(f"{label} must have exactly the eight probe result fields")
    argv = result.get("argv")
    if not _is_list_str(argv):
        raise ValueError(f"{label} argv must be a list of strings")
    exit_code = result.get("exit_code")
    if not _is_int_not_bool(exit_code):
        raise ValueError(f"{label} exit_code must be an integer")
    duration = result.get("duration_ms")
    if not _is_nonneg_int(duration):
        raise ValueError(f"{label} duration_ms must be non-negative")
    for key in ("stdout_path", "stderr_path"):
        non_empty_string(result.get(key), f"{label} {key}")
    for key in ("stdout_sha256", "stderr_sha256"):
        digest = result.get(key)
        if not _is_sha256(digest):
            raise ValueError(f"{label} {key} must be a SHA-256 digest")


def as_mapping(value: object, label: str) -> dict[str, object]:
    """Narrow a decoded value to a string-keyed mapping."""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{label} keys must be strings")
        result[key] = item
    return result


def non_empty_string(value: object, label: str) -> str:
    """Require a non-empty string value."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def load_json_object(path: _Path, label: str) -> dict[str, object]:
    """Load a JSON object from a file."""
    try:
        value = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, _json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid JSON: {error}") from error
    return as_mapping(value, label)


def _request(
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: float,
    payload: dict[str, object] | None = None,
) -> tuple[int, _httpx.Response | None, str]:
    """Perform one bounded HTTP request without following redirects."""
    try:
        if method == "POST":
            if payload is None:
                raise ValueError("POST requests require a JSON payload")
            response = _httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
        else:
            response = _httpx.get(
                url,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
    except _httpx.HTTPError as error:
        return 0, None, str(error)
    return response.status_code, response, ""


def _decode_json(
    status: int,
    response: _httpx.Response | None,
    transport_error: str,
) -> tuple[int, object]:
    """Decode a JSON response while preserving transport errors."""
    if response is None:
        return status, {"error": transport_error}
    try:
        body: object = response.json()
    except ValueError as error:
        return status, {"error": f"invalid JSON response: {error}"}
    return status, body


def http_post(
    url: str,
    payload: dict[str, object],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, object]:
    """POST JSON and return the status plus decoded response."""
    status, response, transport_error = _request("POST", url, headers, timeout, payload)
    return _decode_json(status, response, transport_error)


def http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, object]:
    """GET JSON and return the status plus decoded response."""
    status, response, transport_error = _request("GET", url, headers, timeout)
    return _decode_json(status, response, transport_error)


def http_get_text(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str, str]:
    """GET text and return the status, body, and transport error."""
    status, response, transport_error = _request("GET", url, headers, timeout)
    if response is None:
        return status, "", transport_error
    return status, response.text, ""


def error_text(body: object) -> str:
    """Render a bounded error message from a JSON response."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str):
            return error[:2_000]
    return str(body)[:2_000]


def validate_probe_artifact(name: str, artifact: object) -> dict[str, object]:
    """Validate the complete envelope emitted by one review probe."""
    data = as_mapping(artifact, f"probe artifact {name}")
    allowed = {"name", "result", "data"}
    if set(data) != allowed:
        raise ValueError(f"probe artifact {name} must contain exactly {sorted(allowed)}")
    if data.get("name") != name:
        raise ValueError(f"probe artifact name does not match: {name}")
    result = as_mapping(data.get("result"), f"probe artifact {name} result")
    _validate_probe_result_fields(result, f"probe artifact {name} result")
    if result.get("name") != name:
        raise ValueError(f"probe result name does not match: {name}")
    as_mapping(data.get("data"), f"probe artifact {name} data")
    return data


def as_probe_result_record(value: object, label: str) -> ProbeResultRecord:
    """Validate and narrow a value to a ProbeResultRecord."""
    result = as_mapping(value, label)
    _validate_probe_result_fields(result, label)
    # After validation, narrow each field for mypy via TypeGuard checks.
    name = result["name"]
    if not _is_str(name):
        raise TypeError(f"{label} name must be str")
    argv_list = result["argv"]
    if not _is_list_str(argv_list):
        raise TypeError(f"{label} argv must be list[str]")
    exit_code_val = result["exit_code"]
    if not _is_int_not_bool(exit_code_val):
        raise TypeError(f"{label} exit_code must be int")
    duration_val = result["duration_ms"]
    if not _is_nonneg_int(duration_val):
        raise TypeError(f"{label} duration_ms must be non-negative int")
    stdout_path = result["stdout_path"]
    if not _is_str(stdout_path) or not stdout_path:
        raise TypeError(f"{label} stdout_path must be non-empty str")
    stderr_path = result["stderr_path"]
    if not _is_str(stderr_path) or not stderr_path:
        raise TypeError(f"{label} stderr_path must be non-empty str")
    stdout_sha256 = result["stdout_sha256"]
    if not _is_sha256(stdout_sha256):
        raise TypeError(f"{label} stdout_sha256 must be SHA-256")
    stderr_sha256 = result["stderr_sha256"]
    if not _is_sha256(stderr_sha256):
        raise TypeError(f"{label} stderr_sha256 must be SHA-256")
    # Construct the typed dict; mypy accepts this via structural typing.
    return {
        "name": name,
        "argv": argv_list,
        "exit_code": exit_code_val,
        "duration_ms": duration_val,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "stdout_sha256": stdout_sha256,
        "stderr_sha256": stderr_sha256,
    }
