"""#519 item 1: admission returns the reserved DELTA, not the whole set."""

from __future__ import annotations

import builtins

import crawlutron.admission.admission_links as admission_links
from crawlutron.models.normalized_url import NormalizedUrl
from tests._policy_factories import admit, canonical


async def test_admission_returns_the_newly_reserved_delta():
    """The delta is exactly the identities this pass reserved.

    The hot caller threads ``known_delta`` so no pass copies the occupied set whole."""
    owned = {canonical("https://example.test/root")}
    frontier: list[NormalizedUrl] = []
    outcome = await admit(
        ["https://example.test/child", "https://example.test/other", "https://example.test/child"],
        frontier=frontier,
        max_pages=3,
        known=owned,
        respect_robots=False,
    )

    assert outcome.known_delta == frozenset(
        {canonical("https://example.test/child"), canonical("https://example.test/other")}
    )
    # A duplicate candidate contributes nothing to a later delta.
    again = await admit(
        ["https://example.test/child"],
        frontier=frontier,
        max_pages=3,
        known=owned | outcome.known_delta,
        respect_robots=False,
    )
    assert again.known_delta == frozenset()


async def test_delta_union_matches_full_set_threading_member_for_member():
    """Behavioral equivalence over many passes: unioning each pass's delta dedupes
    exactly as re-deriving the occupied set from all admissions would."""
    known_by_delta: set[NormalizedUrl] = {canonical("https://example.test/root")}
    admitted_ever: set[NormalizedUrl] = {canonical("https://example.test/root")}
    for index in range(50):
        links = [
            f"https://example.test/p{index}-a",
            f"https://example.test/p{index}-b",
            f"https://example.test/p{index}-a",
        ]
        front_b: list[NormalizedUrl] = []
        outcome_b = await admit(
            links,
            frontier=front_b,
            max_pages=1000,
            known=known_by_delta,
            respect_robots=False,
        )
        known_by_delta |= outcome_b.known_delta
        admitted_ever.update(canonical(link) for link in links)

    assert known_by_delta == admitted_ever


async def test_scale_probe_delta_path_never_materializes_the_whole_set(monkeypatch):
    """A counting spy on the builtins: across a long probe the delta path builds
    no frozenset over the occupied set at all."""
    from tests._policy_factories import UNRESTRICTED_HOST_POLICY

    real_frozenset = builtins.frozenset
    whole_set_builds: list[int] = []
    seeded = {canonical(f"https://example.test/seed{n}") for n in range(50)}
    probe = {"known": set(seeded)}

    def counting_frozenset(source=(), /):
        built = real_frozenset(source)
        if len(built) >= len(seeded):
            whole_set_builds.append(len(built))
        return built

    monkeypatch.setattr(builtins, "frozenset", counting_frozenset)
    try:
        for index in range(_probe_count()):
            links = [f"https://example.test/x{index}"]
            frontier: list[NormalizedUrl] = []
            outcome = await admission_links.admit_discovered_links(
                links,
                source_depth=0,
                max_depth=1,
                max_pages=0,
                known=probe["known"],
                frontier=frontier,
                make_entry=lambda link: link.normalized_url,
                scope_pattern=None,
                want_document_bodies=False,
                respect_robots=False,
                robots_allows=_admit_all,
                robots_crawl_delay=None,
                commit_crawl_delay=_commit_nothing,
                host_policy=UNRESTRICTED_HOST_POLICY,
            )
            probe["known"] |= outcome.known_delta
    finally:
        monkeypatch.undo()

    assert whole_set_builds == [], f"the delta path copied the whole set: {whole_set_builds[:5]}"
    assert len(probe["known"]) == len(seeded) + _probe_count()


def _probe_count() -> int:
    return 5000


async def _admit_all(_url: NormalizedUrl) -> bool:
    return True


def _commit_nothing(_domain: str, _delay: float) -> None:
    pass
