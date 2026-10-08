"""The watch sweep: one pass rechecks due watches and announces what changed.

The origin is a real HTTP server answering genuine ``304``s, so "did the
revalidation actually revalidate" is answered by the wire rather than by a
stand-in for the engine. The delivery call is the one boundary stubbed: what
a pass announces is the sweeper's own decision, while the funnel that carries
it -- validation, size cap, pinned delivery -- is ``app.api.webhooks``'
contract and is driven end-to-end in ``tests/test_webhook_ssrf.py``.
"""

import asyncio
from typing import Any

import pytest

from app.api import watch_sweep
from app.api.watch_sweep import WatchSweeper, sweep_watches_forever
from app.config.settings import CrawlerSettings
from app.models.content_validators import ContentValidators
from app.models.watch_outcome import WatchOutcome
from tests._assertions import block_private_targets
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests.helpers.server import EtagHandler

NOW = 1_700_000_000.0
INTERVAL = 3600
WEBHOOK = "http://watcher.example.com/hook"
DEAD_ORIGIN = "http://127.0.0.1:9/"


@pytest.fixture
def sweeper(watch_store):
    """A sweeper over this test's store."""
    return WatchSweeper(
        watch_store,
        host_policy=UNRESTRICTED_HOST_POLICY,
        settings=CrawlerSettings.from_mapping(),
    )


@pytest.fixture
def announced(monkeypatch):
    """The announcements a pass handed the delivery funnel, in order."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def _capture(webhook_url: str, payload: dict[str, Any], *, host_policy) -> None:
        calls.append((webhook_url, payload))

    monkeypatch.setattr(watch_sweep, "post_webhook", _capture)
    return calls


@pytest.fixture
def origin_content():
    """Change the origin's representation for one test, restored afterwards."""
    saved = (EtagHandler.etag, EtagHandler.body)

    def change(etag: str, body: bytes) -> None:
        EtagHandler.etag = etag
        EtagHandler.body = body

    yield change
    EtagHandler.etag, EtagHandler.body = saved


def register(watch_store, url: str, *, at: float = 0.0, webhook: str | None = WEBHOOK):
    return watch_store.create(canonical(url), webhook_url=webhook, interval_seconds=INTERVAL, now=at)


def origin_url(server) -> str:
    """The loopback URL of a served fixture."""
    return f"http://127.0.0.1:{server.server_port}/"


async def test_a_first_check_records_what_the_origin_returned_and_announces_it(
    sweeper, watch_store, etag_server, announced
):
    watch = register(watch_store, origin_url(etag_server))

    outcomes = await sweeper.sweep_once(now=NOW)

    assert outcomes == [WatchOutcome.MODIFIED]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.validators.etag == EtagHandler.etag
    assert reloaded.last_changed_at == NOW
    assert reloaded.next_check_at == NOW + INTERVAL
    assert len(announced) == 1
    delivered_url, payload = announced[0]
    assert delivered_url == WEBHOOK
    assert payload["watch_id"] == watch.watch_id
    assert payload["outcome"] == WatchOutcome.MODIFIED


async def test_an_unchanged_origin_is_revalidated_conditionally_and_announces_nothing(
    sweeper, watch_store, etag_server, announced
):
    watch = register(watch_store, origin_url(etag_server))
    await sweeper.sweep_once(now=NOW)
    announced.clear()

    outcomes = await sweeper.sweep_once(now=NOW + INTERVAL)

    # A 304 is the whole point of the validators: the check cost a request and
    # no body, and nothing downstream heard about it.
    assert outcomes == [WatchOutcome.NOT_MODIFIED]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.validators.etag == EtagHandler.etag
    assert reloaded.consecutive_failures == 0
    assert reloaded.next_check_at == NOW + 2 * INTERVAL
    assert announced == []


async def test_a_changed_origin_is_announced_again_with_the_new_validators(
    sweeper, watch_store, etag_server, announced, origin_content
):
    watch = register(watch_store, origin_url(etag_server))
    await sweeper.sweep_once(now=NOW)
    announced.clear()
    origin_content("v2", b"<html><body>changed</body></html>")

    outcomes = await sweeper.sweep_once(now=NOW + INTERVAL)

    assert outcomes == [WatchOutcome.MODIFIED]
    assert watch_store.get(watch.watch_id).validators.etag == "v2"
    assert len(announced) == 1
    assert announced[0][1]["url"] == watch.url


async def test_a_poll_only_watch_is_checked_and_announces_to_nobody(
    sweeper, watch_store, etag_server, announced
):
    register(watch_store, origin_url(etag_server), webhook=None)

    outcomes = await sweeper.sweep_once(now=NOW)

    assert outcomes == [WatchOutcome.MODIFIED]
    assert announced == []


async def test_a_failed_check_keeps_what_it_knew_and_announces_nothing(sweeper, watch_store, announced):
    """A check that reached no verdict is a failure, not a change."""
    watch = register(watch_store, DEAD_ORIGIN)
    watch_store.record_check(
        watch.watch_id,
        outcome=WatchOutcome.NOT_MODIFIED,
        now=0.0,
        next_check_at=INTERVAL,
        validators=ContentValidators(etag='"v1"'),
    )

    outcomes = await sweeper.sweep_once(now=INTERVAL)

    assert outcomes == [WatchOutcome.ERROR]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.validators.etag == '"v1"'
    assert reloaded.consecutive_failures == 1
    assert announced == []


async def test_a_watch_the_live_policy_no_longer_admits_fails_its_check(
    sweeper, watch_store, etag_server, announced, monkeypatch
):
    """The row holds text identity, so the check re-admits against the policy now in force."""
    watch = register(watch_store, origin_url(etag_server))

    block_private_targets(monkeypatch)
    outcomes = await sweeper.sweep_once(now=NOW)

    assert outcomes == [WatchOutcome.ERROR]
    assert watch_store.get(watch.watch_id).consecutive_failures == 1
    assert announced == []


async def test_a_pass_with_nothing_due_checks_nothing(sweeper, watch_store, etag_server, announced):
    register(watch_store, origin_url(etag_server), at=NOW)

    assert await sweeper.sweep_once(now=NOW - 1) == []
    assert announced == []


async def test_the_sweep_loop_passes_on_the_injected_clock_and_stops_on_the_event(watch_store, announced):
    """The loop ends when the deployment is told to, not when a timer happens to expire."""
    watch = register(watch_store, DEAD_ORIGIN)
    stop = asyncio.Event()

    def clock() -> float:
        """Three passes' worth of time, the last one raising the shutdown flag."""
        passes = watch_store.get(watch.watch_id).consecutive_failures + 1
        if passes >= 3:
            stop.set()
        return passes * INTERVAL

    await asyncio.wait_for(
        sweep_watches_forever(
            watch_store,
            host_policy=UNRESTRICTED_HOST_POLICY,
            settings=CrawlerSettings.from_mapping(),
            shutdown_event=stop,
            interval_seconds=0.0,
            clock=clock,
        ),
        timeout=30,
    )

    assert watch_store.get(watch.watch_id).consecutive_failures == 3


async def test_a_pass_that_raises_does_not_end_the_sweep(watch_store, monkeypatch):
    """One watch that makes the crawl raise must not silently stop the registry."""
    register(watch_store, DEAD_ORIGIN)
    attempts: list[int] = []
    stop = asyncio.Event()

    async def _explode(*args, **kwargs):
        attempts.append(1)
        if len(attempts) >= 3:
            stop.set()
        raise RuntimeError("the crawl raised")

    monkeypatch.setattr(watch_sweep, "execute_crawl", _explode)

    await asyncio.wait_for(
        sweep_watches_forever(
            watch_store,
            host_policy=UNRESTRICTED_HOST_POLICY,
            settings=CrawlerSettings.from_mapping(),
            shutdown_event=stop,
            interval_seconds=0.0,
            # The watch is due one interval out, so the clock has to reach it:
            # a pass with nothing due never reaches the crawl at all.
            clock=lambda: INTERVAL,
        ),
        timeout=30,
    )

    assert len(attempts) == 3
