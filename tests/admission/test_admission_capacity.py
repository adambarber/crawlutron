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
    # #520: the slash variant is distinct path text and consumes capacity; the
    # tracking-param variant is the same value and does not.
    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        [
            "https://example.test/child?utm_source=one",
            "https://example.test/child",
            "https://example.test/child/",
            "https://example.test/other",
            "https://example.test/third",
        ],
        frontier=frontier,
        max_pages=3,
        respect_robots=False,
    )

    assert outcome.accepted == 3
    assert [url.raw for url in frontier] == [
        "https://example.test/child",
        "https://example.test/child/",
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


async def test_known_is_read_only_and_known_delta_threads_the_occupied_set():
    """The caller's ``known`` set is never mutated in place (#159); the pass returns
    the DELTA it reserved (#519), so a caller threads the occupied set by unioning
    it instead of re-deriving it from scratch each batch."""
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
    # admission run's own added-set, and the caller stays the owner.
    assert owned == set(known_snapshot)
    # The returned delta is exactly what this pass added, so unioning it threads
    # the occupied set rather than re-scanning the whole job state.
    assert outcome.known_delta == frozenset(
        {
            canonical("https://example.test/child"),
            canonical("https://example.test/other"),
        }
    )


async def _robots_all_admit(_url: NormalizedUrl) -> bool:
    return True


async def _delay_15(_url: NormalizedUrl) -> float | None:
    return 1.5


async def test_crawl_delay_commits_the_canonical_scheduler_key_not_the_bare_host():
    """#456: the delay commit keys the scheduler's registrable-site map, so a
    ``www``-hosted link's floor is the one ``current_delay`` actually reads."""
    committed: list[tuple[str, float]] = []
    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        ["https://www.example.test/child"],
        frontier=frontier,
        robots_crawl_delay=_delay_15,
        robots_allows=_robots_all_admit,
        commit_crawl_delay=lambda domain, delay: committed.append((domain, delay)),
    )

    assert outcome.accepted == 1
    assert committed == [("example.test", 1.5)], (
        f"the delay committed under {committed!r}, not the canonical scheduler "
        f"key the scheduler's readers use (#456)"
    )


async def test_candidate_duplicated_during_the_crawl_delay_await_is_counted():
    """#522: the re-check after the crawl-delay await must COUNT its drop.

    A twin whose identity lands in ``known`` while the check is in flight was
    dropped silently -- ``duplicates_skipped`` lied by one."""
    known: set[NormalizedUrl] = set()
    checked: list[int] = []

    async def allows(url: NormalizedUrl) -> bool:
        return True

    async def crawl_delay(url: NormalizedUrl) -> float | None:
        checked.append(1)
        # Another writer lands the identity while this check is in flight.
        if len(checked) == 1:
            known.add(canonical("https://example.test/twin"))
        return None

    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        ["https://example.test/twin", "https://example.test/twin"],
        frontier=frontier,
        known=known,
        robots_allows=allows,
        robots_crawl_delay=crawl_delay,
        respect_robots=True,
    )

    assert outcome.accepted == 0
    assert outcome.duplicates_skipped == 2, (
        "the post-await re-check dropped its duplicate without counting it"
    )


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
