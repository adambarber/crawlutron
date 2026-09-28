"""The webhook-crawl concurrency semaphore must be bound to the running loop.

A lazily-constructed module-global ``asyncio.Semaphore`` binds to whichever
event loop first uses it; two ``asyncio.run`` calls are two distinct loops, and
a semaphore created on a closed loop cannot be awaited on a live one. The fix
mirrors ``_LoopBoundPooledClient`` (app.crawl.strategies.http): the module
global stays the single slot -- so a suite reset or a patched builder is still
observed -- and the cached instance is rebuilt whenever the running loop
changes.
"""

from __future__ import annotations

import asyncio

import pytest

import app.api.async_crawl_runner as runner


@pytest.fixture
def reset_semaphore():
    """Restore the module globals so each loop-binding test starts clean.

    ``getattr`` defaults keep the fixture working before the loop-tracking
    global exists, so a red phase fails on the assertion rather than on
    fixture setup.
    """
    saved = (
        runner._async_crawl_semaphore,
        getattr(runner, "_async_crawl_semaphore_loop", None),
    )
    try:
        yield
    finally:
        runner._async_crawl_semaphore = saved[0]
        runner._async_crawl_semaphore_loop = saved[1]


def _get_on_a_fresh_loop():
    async def _get():
        return runner._get_async_crawl_semaphore()

    return asyncio.run(_get())


def test_semaphore_is_rebuilt_when_the_running_loop_changes(reset_semaphore):
    """P2-C: two separate loops must not share one loop-bound primitive."""
    first = _get_on_a_fresh_loop()
    second = _get_on_a_fresh_loop()

    assert first is not second, (
        "the semaphore was reused across event loops; it is bound to a loop that is already gone"
    )


def test_semaphore_is_reused_while_the_loop_is_unchanged(reset_semaphore):
    """The rebuild is keyed on loop identity, so one loop keeps one semaphore
    (no churn, no per-acquire allocation)."""
    loop = asyncio.new_event_loop()
    try:

        async def _get():
            return runner._get_async_crawl_semaphore()

        first = loop.run_until_complete(_get())
        second = loop.run_until_complete(_get())
    finally:
        loop.close()

    assert first is second, "the semaphore was rebuilt on an unchanged loop"


def test_resetting_the_module_global_forces_a_rebuild(reset_semaphore):
    """The module global is the reset surface: assigning it back to ``None``
    (suite reset) must be observed by the next acquisition."""
    built = _get_on_a_fresh_loop()
    runner._async_crawl_semaphore = None
    rebuilt = _get_on_a_fresh_loop()

    assert rebuilt is not built, "the module-global reset had no effect"


def test_builder_is_late_bound_so_monkeypatching_is_observed(reset_semaphore, monkeypatch):
    """The builder is looked up by name at build time, not captured, so a test
    patching it is observed once a build actually runs."""
    made: list[asyncio.Semaphore] = []

    def _fake_build():
        semaphore = asyncio.Semaphore(1)
        made.append(semaphore)
        return semaphore

    monkeypatch.setattr(runner, "_build_async_crawl_semaphore", _fake_build)
    runner._async_crawl_semaphore = None

    assert _get_on_a_fresh_loop() is made[0], "the patched builder was bypassed"


def test_semaphore_is_not_constructed_until_first_use(reset_semaphore):
    """Lazy construction: no event-loop dependency exists at import time."""
    runner._async_crawl_semaphore = None

    semaphore = _get_on_a_fresh_loop()

    assert isinstance(semaphore, asyncio.Semaphore)
