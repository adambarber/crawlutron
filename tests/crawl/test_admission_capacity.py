"""Discovered links commit in order and stop at remaining capacity."""

import pytest

from app.models.normalized_url import NormalizedUrl
from tests._policy_factories import admit, canonical
from tests.crawl.test_admission_policy import _no_crawl_delay


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


class TestAPersistedScopeRestrictionFailsClosed:
    """A stored scope restriction that cannot compile must not widen the crawl.

    The request boundary rejects an invalid ``scope_regex`` with
    ``invalid_policy``, so the pattern that reaches a job row compiled when it
    was written. The gap is the resume: the same row, loaded later, compiles
    under today's rules -- and an unparsable pattern resolved to ``None``,
    which every consumer reads as "no restriction declared". A crawl the
    operator restricted then ran unrestricted, silently, which is the one
    outcome a scope policy must never produce.

    ``None`` from the compiler keeps exactly one meaning: no restriction was
    declared. A restriction that is declared but unusable raises, so the job
    fails loudly instead of crawling wide.
    """

    def test_a_declared_pattern_that_cannot_compile_raises(self):
        from app.admission.admission import compile_scope_pattern

        with pytest.raises(ValueError, match="scope_regex"):
            compile_scope_pattern("(((")

    def test_an_undeclared_pattern_still_means_no_restriction(self):
        from app.admission.admission import compile_scope_pattern

        assert compile_scope_pattern(None) is None
        assert compile_scope_pattern("") is None
