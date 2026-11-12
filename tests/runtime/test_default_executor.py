"""The loop's default executor is sized once at startup (#360)."""

import asyncio
import concurrent.futures

import pytest

from crawlutron.runtime.pressure_sizing import default_executor_workers


def test_default_executor_workers_is_derived_and_capped():
    """The bound derives from the box (never below 8, capped at 32) and honors
    an operator override, mirroring doc_worker_count's derivation pattern."""
    workers = default_executor_workers()
    assert 8 <= workers <= 32


def test_default_executor_workers_override(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_DEFAULT_EXECUTOR_WORKERS", "12")
    assert default_executor_workers() == 12
    monkeypatch.setenv("CRAWLUTRON_DEFAULT_EXECUTOR_WORKERS", "junk")
    assert default_executor_workers() >= 8
    monkeypatch.setenv("CRAWLUTRON_DEFAULT_EXECUTOR_WORKERS", "0")
    assert default_executor_workers() >= 8


async def test_lifespan_installs_a_sized_default_executor():
    """Startup sizes the shared asyncio.to_thread pool: a bare loop has no
    default executor installed, so this is red until the lifespan sets one."""
    from crawlutron_rest.main import install_default_executor

    loop = asyncio.get_running_loop()
    # _default_executor is the asyncio-internal slot the lifespan installs
    # through; the reads name that slot, not the public setter.
    executor_slot = getattr(loop, "_default_executor", None)
    try:
        install_default_executor(loop)
        executor_slot = getattr(loop, "_default_executor", None)
        assert isinstance(executor_slot, concurrent.futures.ThreadPoolExecutor)
        assert executor_slot._max_workers == default_executor_workers()
    finally:
        executor_slot = getattr(loop, "_default_executor", None)
        if executor_slot is not None:
            # The private slot reset is the teardown seam the lifespan owns;
            # object.__setattr__ names it (the doubles' seam spelling).
            object.__setattr__(loop, "_default_executor", None)
            executor_slot.shutdown(wait=False, cancel_futures=True)


@pytest.mark.parametrize("bad", [AttributeError, RuntimeError])
async def test_lifespan_executor_failure_is_survivable(monkeypatch, bad):
    """A sizing failure must not take down startup: the loop falls back to
    Python's implicit default rather than refusing to start."""
    from crawlutron_rest import main as app_main

    monkeypatch.setattr(app_main, "default_executor_workers", lambda: (_ for _ in ()).throw(bad()))
    app_main.install_default_executor(asyncio.get_running_loop())
