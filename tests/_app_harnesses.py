"""The one builder every harness app derives from: the app-shape contract. Every harness app mounts the same
middleware stack and exception handlers as ``service_app`` via ``install_error_surface``, so safety wiring
added to the real app cannot silently miss a test surface.
"""

from __future__ import annotations

from fastapi import FastAPI

__all__ = ["harness_app", "mount_concurrency_tracker", "mounted_app"]


def harness_app(*routers, safety_stack: bool = True) -> FastAPI:
    """A FastAPI app over ``routers`` carrying the service's safety stack."""
    from crawlutron.service.caller_auth import CALLER_QUOTA_ADMISSION
    from crawlutron_rest.main import install_error_surface

    app = FastAPI()
    for router in routers:
        app.include_router(router)
    if safety_stack:
        install_error_surface(app)
    if getattr(app.state, "caller_quota_admission", None) is None:
        app.state.caller_quota_admission = CALLER_QUOTA_ADMISSION
    return app


def mounted_app(client) -> FastAPI:
    """The ASGI app behind an httpx client built over an ASGI transport."""
    app = getattr(client._transport, "app", None)
    assert app is not None, "the client must ride an ASGI transport carrying an app"
    return app


def mount_concurrency_tracker(client):
    """Attach the sync-door register to the client's app and return it."""
    from crawlutron.service.caller_concurrency import CallerConcurrencyTracker

    tracker = CallerConcurrencyTracker()
    mounted_app(client).state.concurrency_tracker = tracker
    return tracker
