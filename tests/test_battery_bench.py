"""The benchmark harness preserves every configured flag except the worker count."""

from __future__ import annotations

import sys

import pytest

import scripts.battery_bench as bench

_ADDOPTS = '--strict-markers -m "not (browser or slow or real_pbkdf2)" -n 8 --dist worksteal'


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench, "addopts", lambda: _ADDOPTS)


def test_command_replaces_the_configured_worker_count(configured: None) -> None:
    assert bench.command(4, 30) == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-o",
        'addopts=--strict-markers -m "not (browser or slow or real_pbkdf2)" --dist worksteal',
        "--durations=30",
        "-n",
        "4",
    ]


def test_serial_command_omits_the_worker_flag(configured: None) -> None:
    assert "-n" not in bench.command(None, 10)


def test_serial_and_workers_are_mutually_exclusive(configured: None) -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        bench.options(["--serial", "--workers", "4"])


def test_default_workers_come_from_pytest_ini(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench, "addopts", lambda: _ADDOPTS)
    assert bench.options([]) == (8, 30)
