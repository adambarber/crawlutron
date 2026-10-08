"""Watch store: registration, due-window, and check-recording contracts.

Every clock in here is injected: the store takes ``now`` on every mutating
call, so no test (and no production path) depends on wall time.
"""

import pytest

from app.models.content_validators import ContentValidators
from app.models.watch_outcome import WatchOutcome
from tests._policy_factories import canonical

NOW = 1_000_000.0
INTERVAL = 3600


def register(store, url="https://example.com/feed.xml", *, at=NOW, interval=INTERVAL, webhook=None):
    """Create a watch through the production seam, admitted the way production admits."""
    return store.create(canonical(url), webhook_url=webhook, interval_seconds=interval, now=at)


def validators(etag=None, last_modified=None):
    """The effective validator set a sweep would hand back after a check."""
    return ContentValidators(etag=etag, last_modified=last_modified)


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

    # due is read-only: a sweep that dies before recording leaves the watch
    # due, so a check is at-least-once rather than silently skipped.
    assert [row.watch_id for row in watch_store.due(NOW + 121)] == [early.watch_id]

    both = watch_store.due(NOW + 700)
    assert [row.watch_id for row in both] == [early.watch_id, late.watch_id]


def test_record_check_moves_the_schedule(watch_store):
    state = register(watch_store)
    checked_at = NOW + INTERVAL

    watch_store.record_check(
        state.watch_id,
        outcome=WatchOutcome.NOT_MODIFIED,
        now=checked_at,
        next_check_at=checked_at + INTERVAL,
        validators=validators(etag="v1"),
    )

    reloaded = watch_store.get(state.watch_id)
    assert reloaded.last_checked_at == checked_at
    assert reloaded.last_outcome == WatchOutcome.NOT_MODIFIED
    assert reloaded.validators.etag == "v1"
    assert reloaded.next_check_at == checked_at + INTERVAL
    assert watch_store.due(checked_at) == []
    assert [row.watch_id for row in watch_store.due(checked_at + INTERVAL)] == [state.watch_id]


def test_modified_outcome_moves_the_change_clock(watch_store):
    state = register(watch_store)
    first_seen = NOW + INTERVAL

    watch_store.record_check(
        state.watch_id,
        outcome=WatchOutcome.MODIFIED,
        now=first_seen,
        next_check_at=first_seen + INTERVAL,
        validators=validators(),
    )
    later = first_seen + INTERVAL
    watch_store.record_check(
        state.watch_id,
        outcome=WatchOutcome.NOT_MODIFIED,
        now=later,
        next_check_at=later + INTERVAL,
        validators=validators(),
    )

    # The change clock holds the last modified check, not the last check.
    assert watch_store.get(state.watch_id).last_changed_at == first_seen


def test_error_outcome_extends_the_failure_streak_and_success_resets_it(watch_store):
    state = register(watch_store)

    for attempt in range(2):
        watch_store.record_check(
            state.watch_id,
            outcome=WatchOutcome.ERROR,
            now=NOW + INTERVAL + attempt,
            next_check_at=NOW + 2 * INTERVAL,
            validators=validators(),
        )
    assert watch_store.get(state.watch_id).consecutive_failures == 2

    watch_store.record_check(
        state.watch_id,
        outcome=WatchOutcome.NOT_MODIFIED,
        now=NOW + 2 * INTERVAL,
        next_check_at=NOW + 3 * INTERVAL,
        validators=validators(),
    )
    assert watch_store.get(state.watch_id).consecutive_failures == 0


def test_validator_replacement_is_wholesale(watch_store):
    """A 200 whose origin dropped the ETag clears it; no merge resurrects it."""
    state = register(watch_store)
    watch_store.record_check(
        state.watch_id,
        outcome=WatchOutcome.MODIFIED,
        now=NOW,
        next_check_at=NOW + INTERVAL,
        validators=validators(etag="v1"),
    )
    watch_store.record_check(
        state.watch_id,
        outcome=WatchOutcome.MODIFIED,
        now=NOW + INTERVAL,
        next_check_at=NOW + 2 * INTERVAL,
        validators=validators(),
    )

    assert watch_store.get(state.watch_id).validators.etag is None


def test_delete_reports_whether_the_watch_existed(watch_store):
    state = register(watch_store)

    assert watch_store.delete(state.watch_id) is True
    assert watch_store.get(state.watch_id) is None
    assert watch_store.delete(state.watch_id) is False


def test_get_of_unknown_watch_is_none(watch_store):
    assert watch_store.get("nonexistent") is None


def test_non_positive_interval_is_rejected(watch_store):
    with pytest.raises(ValueError):
        register(watch_store, interval=0)
