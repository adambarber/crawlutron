"""Browser pool sizing: derive pool size from the container RAM budget.

Adversarial perf fix: a single global pool (one uvicorn worker) whose size is
derived from the cgroup memory limit, so the total browser count stays bounded
and leaves headroom for the cheap HTTP path (instead of 1 pool per worker that
multiplies browsers x workers and pegs RAM).

The RAM-budget math (derive_browser_pool_size + constants) lives in
app.resource_pressure as the single source of truth shared with the httpx pool
and the weighted admission, so the test patches read_memory_stats there.
"""

import os

import pytest

import app.crawl.strategies.browser_pool as bp
from app import resource_pressure as rp


class _Mem:
    def __init__(self, total_bytes):
        self.total_bytes = total_bytes
        self.used_bytes = 0


@pytest.fixture(autouse=True)
def _clear_pool_env(monkeypatch):
    # Ensure no explicit override leaks between tests.
    monkeypatch.delenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", raising=False)
    yield


def test_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", "3")
    assert bp._derive_browser_pool_size() == 3


def test_explicit_override_rejects_garbage(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", "not-a-number")
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    # garbled override -> falls through to budget derivation (3GiB -> 2)
    assert bp._derive_browser_pool_size() == 2


def test_budget_derivation_3g_defaults(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    # 0.5 * 3GiB / 700MiB ~= 2.19 -> floor 2
    assert bp._derive_browser_pool_size() == 2


def test_budget_derivation_capped(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(64 * 1024**3))
    # huge host -> capped at _MAX_DERIVED_POOL_SIZE (4)
    assert bp._derive_browser_pool_size() == rp._MAX_DERIVED_POOL_SIZE


def test_no_limit_falls_back_to_one(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(0))
    assert bp._derive_browser_pool_size() == 1


def test_pool_uses_derived_size(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    pool = bp.BrowserPool()
    assert pool._pool_size == 2


def test_explicit_pool_size_arg_wins(monkeypatch):
    monkeypatch.setattr(rp, "read_memory_stats", lambda: _Mem(3 * 1024**3))
    pool = bp.BrowserPool(pool_size=1)
    assert pool._pool_size == 1
