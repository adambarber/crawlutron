"""#134: discovery links that fail URL admission are counted, not hidden."""

import asyncio

from crawlutron.admission.admission_links import AdmissionOutcome, admit_discovered_links
from crawlutron.safety.validation import HostPolicy


def _run(links: list[object]) -> AdmissionOutcome:
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
            robots_allows=lambda url: True,
            robots_crawl_delay=None,
            commit_crawl_delay=lambda url, delay: None,
            host_policy=HostPolicy.unrestricted(),
        )

    return asyncio.run(_go())


def test_clean_links_admit_and_the_field_stays_zero():
    outcome = _run(["https://example.com/a", "https://example.com/b"])
    assert outcome.accepted == 2
    assert outcome.invalid_skipped == 0


def test_a_link_that_fails_url_admission_is_counted():
    """The stream's None path (admission refusal) lands in invalid_skipped."""
    broken = {"url": "ht!tp://not a url", "normalized_url": "ht!tp://not a url"}
    outcome = _run(["https://example.com/a", broken])
    assert outcome.accepted == 1
    assert outcome.invalid_skipped == 1
