"""The debug/test-hook env knobs are deleted surface, not defaults (#359).
Removed reads: CRAWLUTRON_TRACE_STORE_LOCKS, CRAWLUTRON_TRACE_STORE_CONNECTIONS,
CRAWLUTRON_LOOP_LAG_PROBE, CRAWLUTRON_TEST_HOOKS (TEST_MARKITDOWN_*)."""

from __future__ import annotations

import pytest

import crawlutron.runtime.pressure_probes as pressure_probes
import crawlutron.storage.sqlite_store as sqlite_store
from crawlutron.extraction.doc_pool import doc_task_hooks
from crawlutron.extraction.doc_pool import test_hooks_enabled as hooks_seam_enabled


def _assert_no_trace_env_readers(monkeypatch, env_var: str, module):
    monkeypatch.setenv(env_var, "1")
    module_envs = [name for name in vars(module) if "TRACE" in name.upper()]
    assert module_envs == [], f"sqlite_store still carries trace-env readers: {module_envs}"


@pytest.mark.parametrize(
    "env_var",
    ["CRAWLUTRON_TRACE_STORE_LOCKS", "CRAWLUTRON_TRACE_STORE_CONNECTIONS"],
)
def test_trace_store_knobs_are_not_read(monkeypatch, env_var):
    _assert_no_trace_env_readers(monkeypatch, env_var, sqlite_store)


def test_loop_lag_probe_knob_is_not_read(monkeypatch):
    # The probe is injected through ResourceGate's parameters now, so the env
    # gate name must not survive as a module constant either.
    monkeypatch.setenv("CRAWLUTRON_LOOP_LAG_PROBE", "1")
    module_envs = [
        name
        for name in vars(pressure_probes)
        if "LOOP_LAG_PROBE" in name.upper() and not name.startswith("_")
    ]
    assert module_envs == [], f"pressure_probes still carries an env-gated probe flag: {module_envs}"


def test_test_hooks_knob_is_not_read(monkeypatch):
    # Doc-pool death simulation is fixture-injected; no env gate exists.
    monkeypatch.setenv("CRAWLUTRON_TEST_HOOKS", "1")
    assert doc_task_hooks() is None, "doc_task_hooks still answered to CRAWLUTRON_TEST_HOOKS"
    assert hooks_seam_enabled() is False, "test_hooks_enabled still answered to CRAWLUTRON_TEST_HOOKS"
