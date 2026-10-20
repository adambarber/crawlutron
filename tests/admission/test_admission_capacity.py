"""Discovered links commit in order and stop at remaining capacity."""

import pytest

from app.models.normalized_url import NormalizedUrl
from tests._policy_factories import admit, canonical
from tests.admission.test_admission_policy import _no_crawl_delay


async def test_discovered_link_delay_commits_in_order_and_breaks_at_cap():
    events: list[tuple] = []
    links = [
        "https://example.test/first",
        "https://example.test/over-cap",
        "https://example.test/not-processed",
    ]

    async def allows(url: NormalizedUrl) -> bool:
        events.append(("robots", url.raw))
        return True

    async def crawl_delay(url: NormalizedUrl) -> float:
        events.append(("delay", url.raw))
        return 1.5

    def commit(domain: str, delay: float) -> None:
        events.append(("commit", domain, delay))

    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        links,
        frontier=frontier,
        max_pages=1,
        robots_allows=allows,
        robots_crawl_delay=crawl_delay,
        commit_crawl_delay=commit,
    )

    assert outcome.accepted == 1
    assert outcome.cap_blocked is True
    assert [url.raw for url in frontier] == ["https://example.test/first"]
    assert [event for event in events if event[0] == "delay"] == [
        ("delay", "https://example.test/first"),
        ("delay", "https://example.test/over-cap"),
    ]
    assert [event for event in events if event[0] == "commit"] == [
        ("commit", "example.test", 1.5),
        ("commit", "example.test", 1.5),
    ]
    assert not any(event == ("robots", "https://example.test/not-processed") for event in events)


async def test_duplicate_canonical_spellings_do_not_consume_capacity():
    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        [
            "https://example.test/child?utm_source=one",
            "https://example.test/child/",
            "https://example.test/other",
            "https://example.test/third",
        ],
        frontier=frontier,
        max_pages=2,
        respect_robots=False,
    )

    assert outcome.accepted == 2
    assert [url.raw for url in frontier] == [
        "https://example.test/child",
        "https://example.test/other",
    ]
    assert outcome.cap_blocked is True


async def test_admission_robots_work_stops_at_remaining_capacity():
    calls: list[str] = []

    async def allows(url: NormalizedUrl) -> bool:
        calls.append(url.raw)
        return True

    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        [f"https://example.test/page-{index}" for index in range(100)],
        frontier=frontier,
        max_pages=1,
        known={canonical("https://example.test/root")},
        robots_allows=allows,
        robots_crawl_delay=_no_crawl_delay,
    )

    assert calls == ["https://example.test/page-0"]
    assert outcome.accepted == 0
    assert outcome.cap_blocked is True


async def test_known_is_read_only_and_known_after_threads_the_occupied_set():
    """The caller's ``known`` set is never mutated in place (#159); the updated
    occupied set is RETURNED so a caller can thread it across admission passes
    instead of re-deriving it from scratch each batch."""
    owned = {canonical("https://example.test/root")}
    known_snapshot = frozenset(owned)
    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        [
            "https://example.test/child",
            "https://example.test/other",
            "https://example.test/child",
        ],
        frontier=frontier,
        max_pages=3,
        known=owned,
        respect_robots=False,
    )

    # The set the caller handed in is untouched: dedupe and reserve live on the
    # admission run's own occupied set, and the caller stays the owner.
    assert owned == set(known_snapshot)
    # The returned occupied set reflects everything admitted, so a second pass
    # can seed its dedupe from it rather than re-scanning the whole job state.
    assert outcome.known_after == known_snapshot | {
        canonical("https://example.test/child"),
        canonical("https://example.test/other"),
    }


class TestAPersistedScopeRestrictionFailsClosed:
    """A stored scope restriction that cannot compile must not widen the crawl.

    On resume an unparsable pattern read as "no restriction" and the crawl ran
    unrestricted; an unusable restriction now raises loudly instead.
    """

    def test_a_declared_pattern_that_cannot_compile_raises(self):
        from app.admission.admission_links import compile_scope_pattern

        with pytest.raises(ValueError, match="scope_regex"):
            compile_scope_pattern("(((")

    def test_an_undeclared_pattern_still_means_no_restriction(self):
        from app.admission.admission_links import compile_scope_pattern

        assert compile_scope_pattern(None) is None
        assert compile_scope_pattern("") is None
