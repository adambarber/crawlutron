"""Stubbing the heavy lifespan teardown so a test can run the real lifespan.

Running ``app.router.lifespan_context`` for real starts browsers, HTTP clients
and subprocess pools. A test that only cares about one shutdown step still has
to survive the others, and two files were each assembling their own version of
"neutralise everything that is not the step under test" -- with their own idea
of which closers that covered, so a teardown step added later reached the real
implementation in one file and not the other.
"""

from typing import Any


def stub_heavy_teardown(monkeypatch) -> Any:
    """Neutralise the lifespan's heavy teardown; return the fresh readiness state.

    Replaces every close/close-all the lifespan reaches with a no-op and
    installs a fresh ``READINESS_STATE``, so startup and shutdown run for real
    and only the pool or sampler under test does anything. Returns the fresh
    readiness state, which a sampler test asserts against.
    """
    from app import main as main_mod
    from app.api import job_runner as job_runner_mod
    from app.crawl.strategies import http_client_pool as http_mod
    from app.health import readiness as readiness_mod
    from app.health.readiness import ReadinessState

    async def _noop() -> None:
        return None

    fresh = ReadinessState()
    monkeypatch.setattr(readiness_mod, "READINESS_STATE", fresh)
    monkeypatch.setattr(job_runner_mod, "shutdown_job_runner", _noop)
    monkeypatch.setattr(http_mod, "close_async_http_client", _noop)
    monkeypatch.setattr(http_mod, "close_cffi_session", _noop)
    monkeypatch.setattr(main_mod, "close_browser_pool", _noop)
    return fresh
