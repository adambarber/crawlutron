"""Neutralise the heavy lifespan teardown so a test can run the real lifespan,
which otherwise spawns browsers, clients and pools; this is the one place that
spells which closers the step-under-test is meant to survive.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

    from crawlutron.health.readiness import ReadinessState


def stub_heavy_teardown(monkeypatch: pytest.MonkeyPatch) -> ReadinessState:
    """Neutralise the lifespan's heavy teardown -- each close it reaches is a no-op
    and a fresh ``READINESS_STATE`` is installed -- and return that state, which
    a sampler test asserts against.
    """
    from crawlutron import main as main_mod
    from crawlutron.api import job_runner as job_runner_mod
    from crawlutron.health import readiness as readiness_mod
    from crawlutron.health.readiness import ReadinessState
    from crawlutron.transport import http_client_pool as http_mod

    async def _noop() -> None:
        return None

    fresh = ReadinessState()
    monkeypatch.setattr(readiness_mod, "READINESS_STATE", fresh)
    monkeypatch.setattr(job_runner_mod, "shutdown_job_runner", _noop)
    monkeypatch.setattr(http_mod, "close_async_http_client", _noop)
    monkeypatch.setattr(http_mod, "close_cffi_session", _noop)
    monkeypatch.setattr(main_mod, "close_browser_pool", _noop)
    return fresh
