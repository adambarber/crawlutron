"""The origin is a real HTTP server answering genuine ``304``s, so revalidation
is answered by the wire; the delivery call is the one stub here (the sweeper's
decision), while the funnel it rides is ``app.api.webhooks``' contract.
"""

import asyncio
from typing import Any

import pytest

from app.api import watch_sweep
from app.api.caller_concurrency import CallerConcurrencyTracker
from app.api.watch_sweep import WatchSweeper, sweep_watches_forever
from app.config.settings import CrawlerSettings
from app.models.normalized_url import NormalizedUrl
from app.models.watch_state import WatchState
from app.models.watch_status import WatchStatus
from app.storage.caller_key_store import CallerKeyStore
from app.vocabulary.taxonomy import CrawlClassification
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

    monkeypatch.setattr(watch_sweep, "post_webhook_guarded", _capture)
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


def register(
    watch_store,
    url: NormalizedUrl,
    *,
    at: float = 0.0,
    webhook: str | None = WEBHOOK,
    owner: str = "",
) -> WatchState:
    return watch_store.create(url, webhook_url=webhook, interval_seconds=INTERVAL, now=at, owner=owner)


async def _prime(sweeper, watch_store, server, announced) -> WatchState:
    """Register a watch against ``server``, run its first conditional check, and
    clear the announcement buffer so the follow-up check reports a fresh outcome.
    """
    watch = register(watch_store, canonical(origin_url(server)))
    await sweeper.sweep_once(now=NOW)
    announced.clear()
    return watch


def origin_url(server) -> str:
    """The loopback URL of a served fixture."""
    return f"http://127.0.0.1:{server.server_port}/"


async def test_a_first_check_records_what_the_origin_returned_and_announces_it(
    sweeper, watch_store, etag_server, announced
):
    watch = register(watch_store, canonical(origin_url(etag_server)))

    outcomes = await sweeper.sweep_once(now=NOW)

    assert outcomes == [CrawlClassification.rendered]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.validators.etag == EtagHandler.etag
    assert reloaded.last_changed_at == NOW
    assert reloaded.next_check_at == NOW + INTERVAL
    assert len(announced) == 1
    delivered_url, payload = announced[0]
    assert delivered_url == WEBHOOK
    assert payload["watch_id"] == watch.watch_id
    assert payload["outcome"] == CrawlClassification.rendered


async def test_an_unchanged_origin_is_revalidated_conditionally_and_announces_nothing(
    sweeper, watch_store, etag_server, announced
):
    watch = await _prime(sweeper, watch_store, etag_server, announced)

    outcomes = await sweeper.sweep_once(now=NOW + INTERVAL)

    # A 304 is the whole point of the validators: the check cost a request and no body, and nothing downstream
    # heard about it.
    assert outcomes == [CrawlClassification.not_modified]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.validators.etag == EtagHandler.etag
    assert reloaded.consecutive_failures == 0
    assert reloaded.next_check_at == NOW + 2 * INTERVAL
    assert announced == []


async def test_a_changed_origin_is_announced_again_with_the_new_validators(
    sweeper, watch_store, etag_server, announced, origin_content
):
    watch = await _prime(sweeper, watch_store, etag_server, announced)
    origin_content("v2", b"<html><body>changed</body></html>")

    outcomes = await sweeper.sweep_once(now=NOW + INTERVAL)

    assert outcomes == [CrawlClassification.rendered]
    assert watch_store.get(watch.watch_id).validators.etag == "v2"
    assert len(announced) == 1
    assert announced[0][1]["url"] == watch.url


async def test_a_poll_only_watch_is_checked_and_announces_to_nobody(
    sweeper, watch_store, etag_server, announced
):
    register(watch_store, canonical(origin_url(etag_server)), webhook=None)

    outcomes = await sweeper.sweep_once(now=NOW)

    assert outcomes == [CrawlClassification.rendered]
    assert announced == []


async def test_a_failed_check_keeps_what_it_knew_and_announces_nothing(sweeper, watch_store, announced):
    """A check that reached no verdict is a failure, not a change."""
    watch = register(watch_store, canonical(DEAD_ORIGIN))
    watch_store.record_check(
        watch.watch_id,
        outcome=CrawlClassification.rendered,
        now=0.0,
        next_check_at=INTERVAL,
        etag='"v1"',
    )

    outcomes = await sweeper.sweep_once(now=INTERVAL)

    assert outcomes == [CrawlClassification.robots_disallowed]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.validators.etag == '"v1"'
    assert reloaded.consecutive_failures == 1
    assert announced == []


async def test_a_watch_the_live_policy_no_longer_admits_fails_its_check(
    sweeper, watch_store, etag_server, announced, monkeypatch
):
    """The row holds text identity, so the check re-admits against the policy now in force."""
    watch = register(watch_store, canonical(origin_url(etag_server)))

    block_private_targets(monkeypatch)
    outcomes = await sweeper.sweep_once(now=NOW)

    assert outcomes == [CrawlClassification.unknown_error]
    assert watch_store.get(watch.watch_id).consecutive_failures == 1
    assert announced == []


async def test_a_pass_with_nothing_due_checks_nothing(sweeper, watch_store, etag_server, announced):
    register(watch_store, canonical(origin_url(etag_server)), at=NOW)

    assert await sweeper.sweep_once(now=NOW - 1) == []
    assert announced == []


async def test_the_sweep_loop_passes_on_the_injected_clock_and_stops_on_the_event(watch_store, announced):
    """The loop ends when the deployment is told to, not when a timer happens to expire."""
    watch = register(watch_store, canonical(DEAD_ORIGIN))
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
    register(watch_store, canonical(DEAD_ORIGIN))
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
            # Each pass advances past the rescheduled ``next_check_at`` of a
            # raising watch, so the clock follows the attempt counter to stay due.
            clock=lambda: (len(attempts) + 1) * INTERVAL,
        ),
        timeout=30,
    )

    assert len(attempts) == 3
    # The per-watch isolation rescheduled and flagged the raising watch rather than
    # leaving it due forever or ending the registry.
    assert watch_store.get(watch_store.list_watches()[0].watch_id).consecutive_failures == 3


async def test_a_raising_watch_does_not_starve_a_co_scheduled_watch(sweeper, watch_store, monkeypatch):
    """One due watch whose check raises must not prevent the watch sorted after it
    from being checked in the same pass (#228 / #207)."""
    blocking = register(watch_store, canonical(DEAD_ORIGIN), owner="key_alice")
    # A healthy watch -- the origin really answers, but the crawl raises first.
    healthy = register(watch_store, canonical(WEBHOOK))

    calls: list[str] = []

    async def _explode(payload, *, settings, shutdown_event, urls=None):
        calls.append(payload.url)
        if payload.url == blocking.url:
            raise RuntimeError("blocking watch flow crashed")
        return {"request_id": "x", "submitted_count": 1, "policy_schema_version": "", "results": []}

    monkeypatch.setattr(watch_sweep, "execute_crawl", _explode)

    outcomes = await sweeper.sweep_once(now=NOW)

    # Both due watches were reached -- the raising one did not cut the pass short.
    assert set(calls) == {blocking.url, healthy.url}
    # The isolated failure is recorded as an error; the survivor is rescheduled.
    assert CrawlClassification.unknown_error in outcomes
    blocked_after = watch_store.get(blocking.watch_id)
    assert blocked_after.consecutive_failures == 1
    # The isolated failure was rescheduled to its next natural check, not hot-looped.
    assert blocked_after.next_check_at == NOW + INTERVAL


async def test_a_revoked_owner_watch_is_deactivated_by_the_sweep(watch_store, tmp_path):
    """A watch whose owner key is gone stops issuing checks: the sweep deactivates it
    rather than running it under a dead identity forever (#207)."""
    keys = CallerKeyStore(tmp_path / "keys.db")
    secret = keys.issue_key("alice").secret
    record = keys.verify_key(secret)
    assert record is not None
    owned = register(watch_store, canonical(DEAD_ORIGIN), owner=record.key_id)
    # The anonymous watch, by contrast, has no key to revoke and keeps checking.
    anonymous = register(watch_store, canonical(DEAD_ORIGIN))
    keys.revoke_key(record.key_id)

    sweeper = WatchSweeper(
        watch_store,
        host_policy=UNRESTRICTED_HOST_POLICY,
        settings=CrawlerSettings.from_mapping(),
        key_store=keys,
    )

    outcomes = await sweeper.sweep_once(now=NOW)

    assert CrawlClassification.site_error in outcomes
    revoked = watch_store.get(owned.watch_id)
    assert revoked.status is WatchStatus.DEACTIVATED
    # The anonymous watch has no key to revoke: it was checked just like before.
    anonymous_after = watch_store.get(anonymous.watch_id)
    assert anonymous_after.status is WatchStatus.ACTIVE
    assert anonymous_after.last_checked_at is not None


def _keyed_sweeper(watch_store, tmp_path, monkeypatch, *, capture: list[str] | None = None):
    """A sweeper over a keyed owner (``max_concurrency=1``) with its register and
    a recording ``execute_crawl``; returns ``(tracker, record, sweeper)``.
    """
    keys = CallerKeyStore(tmp_path / "keys.db")
    record = keys.verify_key(keys.issue_key("agent-a", max_concurrency=1).secret)
    assert record is not None
    register(watch_store, canonical(DEAD_ORIGIN), owner=record.key_id)
    tracker = CallerConcurrencyTracker()

    async def _exec(payload, *, settings, shutdown_event, urls=None):
        if capture is not None:
            capture.append(payload.url)
        return {"request_id": "x", "submitted_count": 1, "policy_schema_version": "", "results": []}

    monkeypatch.setattr(watch_sweep, "execute_crawl", _exec)
    sweeper = WatchSweeper(
        watch_store,
        host_policy=UNRESTRICTED_HOST_POLICY,
        settings=CrawlerSettings.from_mapping(),
        key_store=keys,
        concurrency_tracker=tracker,
        job_store=None,
    )
    return tracker, record, sweeper


async def test_a_busy_owner_watch_check_is_refused_not_run_unbounded(watch_store, tmp_path, monkeypatch):
    """A due watch whose owner is at their concurrency cap is refused at admission
    rather than executing a check outside the unified concurrency register (#325)."""
    calls: list[str] = []
    tracker, record, sweeper = _keyed_sweeper(watch_store, tmp_path, monkeypatch, capture=calls)
    tracker.reserve(record.key_id)  # the owner's one slot is already taken
    watch = watch_store.list_watches()[0]

    outcomes = await sweeper.sweep_once(now=NOW)

    # The check never ran: the owner's concurrency cap refused it at admission.
    assert calls == []
    assert outcomes == [CrawlClassification.unknown_error]
    assert watch_store.get(watch.watch_id).consecutive_failures == 1


async def test_a_owner_watch_check_releases_the_slot_after_a_check(watch_store, tmp_path, monkeypatch):
    """The concurrency slot a watch check reserves is released on completion (#325)."""
    tracker, record, sweeper = _keyed_sweeper(watch_store, tmp_path, monkeypatch)

    await sweeper.sweep_once(now=NOW)

    # A check that completed released its owner slot rather than leaking it.
    assert tracker.sync_in_flight(record.key_id) == 0
