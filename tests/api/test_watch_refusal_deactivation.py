"""Terminal refusals deactivate a watch: one concept, four refusal causes.

The sweep's deactivation decision (same lifecycle as a revoked owner) for a
watch the live policy no longer admits, robots refusal, or a foreign refusal
reason; and its negation -- a transient failure deactivates nothing.
"""

import pytest

from crawlutron.api import watch_sweep
from crawlutron.api.crawl_executor import CrawlRefusalError
from crawlutron.models.watch_status import WatchStatus
from crawlutron.vocabulary.taxonomy import CrawlClassification
from tests._assertions import block_private_targets
from tests._policy_factories import canonical
from tests._watch_harnesses import (
    WATCH_INTERVAL,
    capture_announcements,
    default_sweeper,
    loopback_url,
    register_watch,
    stub_transient_failure,
)

NOW = 1_700_000_000.0
DEAD_ORIGIN = "http://127.0.0.1:9/"


@pytest.fixture
def announced(monkeypatch):
    return capture_announcements(monkeypatch)


async def test_a_watch_the_live_policy_no_longer_admits_is_deactivated(
    watch_store, etag_server, announced, monkeypatch
):
    """The row holds text identity, so the check re-admits against the policy now in
    force; a host the deployment now refuses is terminal, not a transient failure."""
    watch = register_watch(watch_store, canonical(loopback_url(etag_server)))

    block_private_targets(monkeypatch)
    outcomes = await default_sweeper(watch_store).sweep_once(now=NOW)

    assert outcomes == [CrawlClassification.private_network_target]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.status is WatchStatus.DEACTIVATED
    assert reloaded.last_outcome is CrawlClassification.private_network_target
    assert announced == []
    assert watch_store.due(NOW + WATCH_INTERVAL) == []


async def test_a_host_denied_watch_is_deactivated_and_the_pass_survives(
    watch_store, etag_server, announced, monkeypatch
):
    """The headline scenario: an operator denies the watched host. The refusal
    reason crosses the executor as data, so the sweep deactivates the row with
    host_denied recorded AND the pass still serves a co-scheduled watch -- the
    enum-cast defect raised out of the except arm and starved them all."""
    denied_watch = register_watch(watch_store, canonical(loopback_url(etag_server)), owner="key_d")
    healthy = register_watch(watch_store, canonical(DEAD_ORIGIN))

    async def _refuse(payload, *, settings, shutdown_event, urls=None):
        if payload.url == denied_watch.url:
            raise CrawlRefusalError("host_denied")
        return {"request_id": "x", "submitted_count": 1, "policy_schema_version": "", "results": []}

    monkeypatch.setattr(watch_sweep, "execute_crawl", _refuse)
    outcomes = await default_sweeper(watch_store).sweep_once(now=NOW)

    # Both watches got a verdict: the refusal was terminal for its row only, and
    # the survivor rode the transient path instead of being starved by the arm.
    assert outcomes == [CrawlClassification.host_denied, CrawlClassification.unknown_error]
    denied_after = watch_store.get(denied_watch.watch_id)
    assert denied_after.status is WatchStatus.DEACTIVATED
    assert denied_after.last_outcome is CrawlClassification.host_denied
    # The refusal was terminal only for the refused row; the survivor stays due.
    assert [w.watch_id for w in watch_store.due(NOW + WATCH_INTERVAL)] == [healthy.watch_id]
    assert watch_store.get(healthy.watch_id).status is WatchStatus.ACTIVE


async def test_a_foreign_refusal_reason_deactivates_with_the_named_fallback(
    watch_store, announced, monkeypatch
):
    """A refusal reason outside the taxonomy must not crash the sweep's except
    arm (the enum-cast defect) nor be silently dropped: the row deactivates with
    unknown_error recorded -- the NAMED fallback, never a cast of foreign text."""
    watch = register_watch(watch_store, canonical(DEAD_ORIGIN))

    async def _foreign_refusal(payload, *, settings, shutdown_event, urls=None):
        raise CrawlRefusalError("host_forbidden_by_new_rule")

    monkeypatch.setattr(watch_sweep, "execute_crawl", _foreign_refusal)
    outcomes = await default_sweeper(watch_store).sweep_once(now=NOW)

    assert outcomes == [CrawlClassification.unknown_error]
    reloaded = watch_store.get(watch.watch_id)
    # The refused row is gone from the schedule; the foreign reason degrades to
    # the NAMED unknown_error fallback, recorded rather than cast.
    assert (reloaded.status, reloaded.last_outcome) == (
        WatchStatus.DEACTIVATED,
        CrawlClassification.unknown_error,
    )
    assert watch_store.due(NOW + WATCH_INTERVAL) == []


async def test_a_transient_check_failure_is_not_a_deactivation(watch_store, announced, monkeypatch):
    """A transport-level failure keeps the watch active: only an admission refusal
    is terminal, so a dead origin stays scheduled for its next interval."""
    watch = register_watch(watch_store, canonical(DEAD_ORIGIN))
    stub_transient_failure(monkeypatch)

    outcomes = await default_sweeper(watch_store).sweep_once(now=NOW)

    assert outcomes == [CrawlClassification.unknown_error]
    # Alive and scheduled: a transport failure grows the streak, never deactivates.
    reloaded = watch_store.get(watch.watch_id)
    assert (reloaded.status, reloaded.consecutive_failures) == (WatchStatus.ACTIVE, 1)
    assert announced == []


async def test_a_robots_disallowed_watch_is_deactivated(watch_store, announced):
    """A target robots txt now refuses is a terminal watch outcome: the sweep
    deactivates the watch instead of growing its failure streak forever (#335)."""
    watch = register_watch(watch_store, canonical(DEAD_ORIGIN))

    outcomes = await default_sweeper(watch_store).sweep_once(now=NOW)

    assert outcomes == [CrawlClassification.robots_disallowed]
    reloaded = watch_store.get(watch.watch_id)
    assert reloaded.status is WatchStatus.DEACTIVATED
    # A deactivated watch is no longer swept: it is not due next interval.
    assert watch_store.due(NOW + WATCH_INTERVAL) == []
