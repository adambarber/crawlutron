"""Shared primitives for the automated pull-request review contract (package version)."""

from __future__ import annotations

from typing import TypedDict


class ProbeResultRecord(TypedDict):
    name: str
    argv: list[str]
    exit_code: int
    duration_ms: int
    stdout_path: str
    stderr_path: str
    stdout_sha256: str
    stderr_sha256: str


def probe_name(record: ProbeResultRecord) -> str:
    """Return the probe name from a result record."""
    return record["name"]


def probe_argv(record: ProbeResultRecord) -> list[str]:
    """Return the probe argv from a result record."""
    return record["argv"]


def probe_exit_code(record: ProbeResultRecord) -> int:
    """Return the probe exit code from a result record."""
    return record["exit_code"]


def probe_duration_ms(record: ProbeResultRecord) -> int:
    """Return the probe duration in milliseconds from a result record."""
    return record["duration_ms"]


def probe_stdout_path(record: ProbeResultRecord) -> str:
    """Return the probe stdout path from a result record."""
    return record["stdout_path"]


def probe_stderr_path(record: ProbeResultRecord) -> str:
    """Return the probe stderr path from a result record."""
    return record["stderr_path"]


def probe_stdout_sha256(record: ProbeResultRecord) -> str:
    """Return the probe stdout SHA-256 from a result record."""
    return record["stdout_sha256"]


def probe_stderr_sha256(record: ProbeResultRecord) -> str:
    """Return the probe stderr SHA-256 from a result record."""
    return record["stderr_sha256"]
