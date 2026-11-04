"""Watch store: the per-caller cap counts only watches the sweep can run."""

from __future__ import annotations

from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.vocabulary.taxonomy import CrawlClassification
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def _watched_url() -> NormalizedUrl:
    """The one admitted URL every watch in this module registers."""
    return NormalizedUrl("http://example.com/a", policy=UNRESTRICTED_HOST_POLICY)


def test_count_for_owner_counts_only_active_watches(watch_store):
    """A deactivated watch must not consume the caller's registration cap.

    The cap's whole point is to bound live scheduling load; the sweep never
    runs a deactivated row, so counting it lies to the admission gate.
    """
    watch = watch_store.create(_watched_url(), interval_seconds=60, now=1000.0, owner="caller-1")
    assert watch_store.count_for_owner("caller-1") == 1

    watch_store.deactivate(watch.watch_id, now=1010.0, next_check_at=9999.0)

    assert watch_store.count_for_owner("caller-1") == 0
    assert watch_store.count_for_owner("caller-2") == 0


def test_count_for_owner_still_counts_its_owner_and_no_one_else(watch_store):
    """The active-count baseline: ownership filtering stays exactly as it was."""
    watch_store.create(_watched_url(), interval_seconds=60, now=1000.0, owner="caller-1")
    other = NormalizedUrl("http://example.com/b", policy=UNRESTRICTED_HOST_POLICY)
    watch_store.create(other, interval_seconds=60, now=1000.0, owner="caller-2")

    assert watch_store.count_for_owner("caller-1") == 1
    assert watch_store.count_for_owner("caller-2") == 1
    assert watch_store.count_for_owner("caller-3") == 0


def test_a_failed_check_does_not_stop_counting_a_watch(watch_store):
    """A non-terminal failure keeps the watch active and counted; only a
    ``deactivate`` (terminal status) releases the cap slot."""
    watch = watch_store.create(_watched_url(), interval_seconds=60, now=1000.0, owner="caller-1")
    watch_store.record_check(
        watch.watch_id,
        outcome=CrawlClassification.site_error,
        now=1010.0,
        next_check_at=1060.0,
    )

    assert watch_store.count_for_owner("caller-1") == 1
