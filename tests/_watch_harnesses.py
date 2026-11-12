"""The watches API over a test's own store: the real router with store and
settings overridden, lived in its own module because both the endpoint suite and
the store-offload guard need the same app.
"""

from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models import watch_state
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.service import watch_sweep
from crawlutron.service.watch_sweep import WatchSweeper
from crawlutron.vocabulary.taxonomy import CrawlClassification
from crawlutron_rest.api.dependencies import get_settings, get_watch_store
from crawlutron_rest.api.watch_router import router as watch_router
from tests._app_harnesses import harness_app
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

WATCH_INTERVAL = 3600
WATCH_WEBHOOK = "https://watcher.example.com/hook"
# The default registration target; passing target=None means a poll-only watch.
DEFAULT_TARGET = WebhookTarget.admit(WATCH_WEBHOOK, host_policy=UNRESTRICTED_HOST_POLICY)


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


def register_watch(
    watch_store,
    url: NormalizedUrl,
    *,
    at: float = 0.0,
    target: WebhookTarget | None = DEFAULT_TARGET,
    owner: str = "",
) -> watch_state.WatchState:
    """One watch row at the suite's standard interval, through the real store."""
    return watch_store.create(url, webhook_url=target, interval_seconds=WATCH_INTERVAL, now=at, owner=owner)


def stub_transient_failure(monkeypatch) -> None:
    """Stub execute_crawl to a transport-level failure: the transient shape the
    watch must ride out (no admission verdict at all, so nothing deactivates)."""

    async def _fail(payload, *, settings, shutdown_event, urls=None):
        raise ConnectionError("the origin is down")

    monkeypatch.setattr(watch_sweep, "execute_crawl", _fail)


def capture_announcements(monkeypatch):
    """The announcements a pass handed the delivery funnel, in order; the spawn
    seam is stubbed so tests observe the completed delivery."""
    calls: list[tuple[str, dict[str, object]]] = []

    async def _capture(target: object, payload: dict[str, object]) -> None:
        calls.append((str(target), payload))

    def _spawn(target: object, payload: dict[str, object]):
        return asyncio.create_task(_capture(target, payload))

    monkeypatch.setattr(watch_sweep, "start_webhook_delivery", _spawn)
    return calls


def watch_client(store) -> AsyncClient:
    """An AsyncClient over ``/v1/watches``, backed by ``store``."""
    app = harness_app(watch_router)
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
    "WATCH_INTERVAL",
    "WATCH_WEBHOOK",
    "DEFAULT_TARGET",
    "capture_announcements",
    "default_sweeper",
    "get_settings",
    "get_watch_store",
    "loopback_url",
    "register_watch",
    "stub_transient_failure",
    "watch_client",
    "watch_router",
    "WebhookTarget",
    "WatchSweeper",
    "watch_sweep",
    "CrawlClassification",
    "UNRESTRICTED_HOST_POLICY",
]
