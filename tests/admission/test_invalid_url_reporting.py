"""#134: discovery links that fail URL admission are counted, not hidden."""

import asyncio

from crawlutron.admission.admission_links import AdmissionOutcome, admit_discovered_links
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.host_policy import HostPolicy as HostPolicy
from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import NormalizedUrl


def _run(links: list[DiscoveredLink | dict[str, JsonValue] | str]) -> AdmissionOutcome:
    async def _go() -> AdmissionOutcome:
        return await admit_discovered_links(
            links,
            source_depth=0,
            max_depth=3,
            max_pages=0,
            known=set(),
            frontier=[],
            make_entry=lambda link: link.normalized_url,
            scope_pattern=None,
            want_document_bodies=False,
            respect_robots=False,
            robots_allows=_allowed_sync,
            robots_crawl_delay=None,
            commit_crawl_delay=lambda url, delay: None,
            host_policy=HostPolicy.unrestricted(),
        )

    return asyncio.run(_go())


async def _allowed_sync(url: NormalizedUrl) -> bool:
    """RobotsAllows is async by contract; the test's allow-all is too."""
    return True


def test_clean_links_admit_and_the_field_stays_zero():
    outcome = _run(["https://example.com/a", "https://example.com/b"])
    assert outcome.accepted == 2
    assert outcome.invalid_skipped == 0


def test_a_link_that_fails_url_admission_is_counted():
    """The stream's None path (admission refusal) lands in invalid_skipped."""
    broken: dict[str, JsonValue] = {"url": "ht!tp://not a url", "normalized_url": "ht!tp://not a url"}
    outcome = _run(["https://example.com/a", broken])
    assert outcome.accepted == 1
    assert outcome.invalid_skipped == 1
