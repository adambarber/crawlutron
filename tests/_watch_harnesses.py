"""The watches API over a test's own store: the real router with store and
settings overridden, lived in its own module because both the endpoint suite and
the store-offload guard need the same app.
"""

from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crawlutron.api.dependencies import get_settings, get_watch_store
from crawlutron.api.watch_router import router as watch_router
from crawlutron.api.watch_sweep import WatchSweeper
from crawlutron.config.settings import CrawlerSettings
from tests._app_factory import service_app
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def loopback_url(server) -> str:
    """The loopback URL of a served fixture."""
    return f"http://127.0.0.1:{server.server_port}/"


def default_sweeper(watch_store) -> WatchSweeper:
    """A sweeper over this test's store, under the unrestricted host policy."""
    return WatchSweeper(
        watch_store,
        host_policy=UNRESTRICTED_HOST_POLICY,
        settings=CrawlerSettings.from_mapping(),
    )


def watch_client(store) -> AsyncClient:
    """An AsyncClient over ``/v1/watches``, backed by ``store``."""
    app = FastAPI()
    app.include_router(watch_router)
    # The error envelope is part of the surface under test, so the handlers that shape it are read off the
    # real app rather than restated here: a harness that spelled them out would keep answering with the old
    for exception_class, handler in service_app().exception_handlers.items():
        app.add_exception_handler(exception_class, handler)
    from tempfile import mkdtemp

    from crawlutron.storage.caller_key_store import CallerKeyStore

    app.state.caller_key_store = CallerKeyStore(mkdtemp() + "/keys.db")
    app.dependency_overrides[get_watch_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


__all__ = [
    "ASGITransport",
    "AsyncClient",
    "CrawlerSettings",
    "FastAPI",
    "default_sweeper",
    "get_settings",
    "get_watch_store",
    "loopback_url",
    "service_app",
    "watch_client",
    "watch_router",
]
