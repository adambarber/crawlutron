"""Watch store: registration, due-window, and check-recording contracts.

Every clock in here is injected: the store takes ``now`` on every mutating
call, so no test (and no production path) depends on wall time.
"""

import pytest
from safe_url.host_policy import HostPolicy

from crawlutron.models.watch_status import WatchStatus
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.vocabulary.taxonomy import CrawlClassification
from tests._policy_factories import canonical

NOW = 1_000_000.0
INTERVAL = 3600


def register(store, url="https://example.com/feed.xml", *, at=NOW, interval=INTERVAL, webhook=None):
    """Create a watch through the production seam, admitted the way production admits."""
    target = None if webhook is None else WebhookTarget.admit(webhook, host_policy=HostPolicy.unrestricted())
    return store.create(canonical(url), webhook_url=target, interval_seconds=interval, now=at)


def claimed_record(
    store,
    watch_id,
    *,
    outcome,
    at=NOW,
    next_at=NOW + INTERVAL,
    token,
    claim_at=None,
    **extra,
):
    """Claim the row (at ``claim_at``, defaulting to ``at``) and record
    ``outcome`` under ``token``; the arrange step every verdict test needs."""
    store.claim_due(claim_at if claim_at is not None else at, token, claim_seconds=300.0)
    return store.record_check(
        watch_id,
        outcome=outcome,
        now=at,
        next_check_at=next_at,
        claimed_by=token,
        **extra,
    )


def test_registration_round_trips_what_was_declared(watch_store):
    state = register(watch_store, webhook="https://example.com/hook")

    assert state.url == canonical("https://example.com/feed.xml").raw
    assert state.webhook_url == "https://example.com/hook"
    assert state.interval_seconds == INTERVAL
    # A watch starts one full interval out, with no check behind it.
    assert state.next_check_at == NOW + INTERVAL
    assert state.last_outcome is None
    assert state.consecutive_failures == 0
    assert state.validators.etag is None

    assert [row.watch_id for row in watch_store.list_watches()] == [state.watch_id]


def test_watch_ids_are_distinct_and_resolve_to_their_own_row(watch_store):
    first = register(watch_store, url="https://example.com/a")
    second = register(watch_store, url="https://example.com/b")

    assert first.watch_id != second.watch_id
    assert watch_store.get(first.watch_id).url == canonical("https://example.com/a").raw
    assert watch_store.get(second.watch_id).url == canonical("https://example.com/b").raw


def test_poll_only_watch_persists_a_missing_webhook(watch_store):
    state = register(watch_store)

    assert state.webhook_url is None
    assert watch_store.get(state.watch_id).webhook_url is None


def test_due_selects_matured_watches_earliest_first(watch_store):
    early = register(watch_store, url="https://example.com/early", interval=60)
    late = register(watch_store, url="https://example.com/late", interval=600)

    # Nothing has matured: a watch is never due before its first interval.
    assert watch_store.due(NOW + 30) == []

    due = watch_store.due(NOW + 120)
    assert [row.watch_id for row in due] == [early.watch_id]

    # due is read-only: a sweep that dies before recording leaves the watch due, so a check is at-least-once
    # rather than silently skipped.
    assert [row.watch_id for row in watch_store.due(NOW + 121)] == [early.watch_id]

    both = watch_store.due(NOW + 700)
    assert [row.watch_id for row in both] == [early.watch_id, late.watch_id]


def test_record_check_moves_the_schedule(watch_store):
    state = register(watch_store)
    checked_at = NOW + INTERVAL
    watch_store.claim_due(checked_at, "sweeper-a", claim_seconds=300.0)
    # A prior successful fetch established the etag; the revalidation below kept it
    # and is the one whose schedule move this test reads.
    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.rendered,
        at=NOW,
        next_at=checked_at,
        etag="v1",
    )
    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.not_modified,
        at=checked_at,
        next_at=checked_at + INTERVAL,
    )

    reloaded = watch_store.get(state.watch_id)
    assert reloaded.last_checked_at == checked_at
    assert reloaded.last_outcome == CrawlClassification.not_modified
    assert reloaded.validators.etag == "v1"
    assert reloaded.next_check_at == checked_at + INTERVAL
    assert watch_store.due(checked_at) == []
    assert [row.watch_id for row in watch_store.due(checked_at + INTERVAL)] == [state.watch_id]


def test_modified_outcome_moves_the_change_clock(watch_store):
    state = register(watch_store)
    first_seen = NOW + INTERVAL

    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.rendered,
        at=first_seen,
        next_at=first_seen + INTERVAL,
        claim_at=first_seen,
    )
    later = first_seen + INTERVAL
    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.not_modified,
        at=later,
        next_at=later + INTERVAL,
        claim_at=later,
    )

    # The change clock holds the last modified check, not the last check.
    assert watch_store.get(state.watch_id).last_changed_at == first_seen


def test_error_outcome_extends_the_failure_streak_and_success_resets_it(watch_store):
    state = register(watch_store)

    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.site_error,
        at=NOW + INTERVAL,
        next_at=NOW + 2 * INTERVAL,
        claim_at=NOW + 2 * INTERVAL,
    )
    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.site_error,
        at=NOW + INTERVAL + 1,
        next_at=NOW + 2 * INTERVAL,
        claim_at=NOW + 2 * INTERVAL,
    )
    assert watch_store.get(state.watch_id).consecutive_failures == 2

    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.not_modified,
        at=NOW + 2 * INTERVAL,
        next_at=NOW + 3 * INTERVAL,
        claim_at=NOW + 3 * INTERVAL,
    )
    assert watch_store.get(state.watch_id).consecutive_failures == 0


def test_a_skipped_check_is_not_a_failure(watch_store):
    """A robots-txt refusal is a deliberate non-attempt, not an error: it must not
    grow the failure streak, so a persistent refusal cannot climb monotonically
    without a deactivation path (#335)."""
    state = register(watch_store)
    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.robots_disallowed,
        at=NOW,
        next_at=NOW + INTERVAL,
    )

    assert watch_store.get(state.watch_id).consecutive_failures == 0


def test_validator_replacement_is_wholesale(watch_store):
    """A 200 whose origin dropped the ETag clears it; no merge resurrects it."""
    state = register(watch_store)
    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.rendered,
        at=NOW,
        next_at=NOW + INTERVAL,
        etag="v1",
    )
    claimed_record(
        watch_store,
        state.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.rendered,
        at=NOW + INTERVAL,
        next_at=NOW + 2 * INTERVAL,
    )

    assert watch_store.get(state.watch_id).validators.etag is None


def test_delete_reports_whether_the_watch_existed(watch_store):
    state = register(watch_store)

    assert watch_store.delete(state.watch_id) is True
    assert watch_store.get(state.watch_id) is None
    assert watch_store.delete(state.watch_id) is False


def test_count_for_owner_counts_only_active_watches(watch_store):
    """A deactivated watch must not consume the caller's registration cap: the cap
    bounds live scheduling load, and the sweep never runs a deactivated row."""
    watch = watch_store.create(
        canonical("https://example.com/a"), interval_seconds=INTERVAL, now=NOW, owner="caller-1"
    )
    assert watch_store.count_for_owner("caller-1") == 1

    watch_store.deactivate(watch.watch_id, now=NOW, next_check_at=NOW + 10_000 * INTERVAL)

    assert watch_store.count_for_owner("caller-1") == 0


def test_count_for_owner_still_counts_its_owner_and_no_one_else(watch_store):
    """The active-count baseline: ownership filtering stays exactly as it was."""
    register(watch_store, url="https://example.com/a")
    register(watch_store, url="https://example.com/b")

    assert watch_store.count_for_owner("") == 2
    assert watch_store.count_for_owner("caller-x") == 0


def test_a_failed_check_does_not_stop_counting_a_watch(watch_store):
    """A non-terminal failure keeps the watch active and counted; only a
    ``deactivate`` (terminal status) releases the cap slot."""
    watch = register(watch_store, url="https://example.com/a")
    claimed_record(
        watch_store,
        watch.watch_id,
        token="sweeper-a",
        outcome=CrawlClassification.site_error,
        at=NOW,
        next_at=NOW + INTERVAL,
        claim_at=NOW + INTERVAL,
    )

    assert watch_store.count_for_owner("") == 1


def test_get_of_unknown_watch_is_none(watch_store):
    assert watch_store.get("nonexistent") is None


def test_non_positive_interval_is_rejected(watch_store):
    with pytest.raises(ValueError):
        register(watch_store, interval=0)


def test_deactivate_records_the_terminal_reason_it_was_given(watch_store):
    """The stored reason is the terminal classification the caller carries (#368)."""
    state = register(watch_store)

    watch_store.deactivate(
        state.watch_id,
        now=NOW,
        next_check_at=NOW + INTERVAL,
        reason=CrawlClassification.robots_disallowed,
    )

    reloaded = watch_store.get(state.watch_id)
    assert reloaded.status.name == "DEACTIVATED"
    assert reloaded.last_outcome == CrawlClassification.robots_disallowed, (
        "deactivate hardcoded site_error, losing the robots refusal reason"
    )


@pytest.mark.parametrize(
    "token,expected",
    [
        ("sweeper-a", WatchStatus.DEACTIVATED),
        ("sweeper-b", WatchStatus.ACTIVE),
    ],
)
def test_deactivate_respects_the_claim_token(watch_store, token, expected):
    """A deactivate lands under the row's claim token and is refused under a
    foreign one: the store is the claim-ownership chokepoint (#956)."""
    state = register(watch_store)
    watch_store.claim_due(NOW + INTERVAL, "sweeper-a", claim_seconds=300.0)
    watch_store.deactivate(
        state.watch_id,
        now=NOW + INTERVAL,
        next_check_at=NOW + 2 * INTERVAL,
        claimed_by=token,
    )
    assert watch_store.get(state.watch_id).status is expected
