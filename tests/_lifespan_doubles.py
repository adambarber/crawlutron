"""Lifespan teardown doubles: observe each close and still release the pool.

A test driving the real lifespan must not leave connections behind, so the
closers it substitutes record the call and then run the production closer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from crawlutron.runtime.readiness_state import ReadinessState

if TYPE_CHECKING:
    import pytest


class _ObservedReadiness(ReadinessState):
    """Readiness state that also records the close steps the double performed."""

    def __init__(self) -> None:
        super().__init__()
        self.closed_steps: list[str] = []


def stub_heavy_teardown(monkeypatch: pytest.MonkeyPatch) -> _ObservedReadiness:
    """Install a fresh readiness state and recording, releasing teardown steps.

    The singleton's stores are session-shared; their close is recorded but not
    run, so a sibling test keeps its connections. A test reads ``closed_steps``.
    """
    import crawlutron_rest.main as main_mod
    from crawlutron.runtime import readiness_state as readiness_mod
    from crawlutron.service import job_runner as job_runner_mod
    from crawlutron.service import store_shutdown as store_shutdown_mod
    from crawlutron.transport import http_client_pool as http_mod

    fresh = _ObservedReadiness()

    def _releasing(name: str, real):
        async def _close(*args, **kwargs) -> None:
            fresh.closed_steps.append(name)
            await real(*args, **kwargs)

        return _close

    async def _stores_close(*args, **kwargs) -> None:
        fresh.closed_steps.append("stores")

    monkeypatch.setattr(readiness_mod, "READINESS_STATE", fresh)
    monkeypatch.setattr(
        job_runner_mod, "shutdown_job_runner", _releasing("job_runner", job_runner_mod.shutdown_job_runner)
    )
    monkeypatch.setattr(
        http_mod, "close_async_http_client", _releasing("http_client", http_mod.close_async_http_client)
    )
    monkeypatch.setattr(http_mod, "close_cffi_session", _releasing("cffi", http_mod.close_cffi_session))
    monkeypatch.setattr(main_mod, "close_browser_pool", _releasing("browser", main_mod.close_browser_pool))
    monkeypatch.setattr(store_shutdown_mod, "close_app_stores", _stores_close)
    return fresh
