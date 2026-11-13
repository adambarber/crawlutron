"""#445 red-first: recursive admitted tasks key the scheduler by bare host
while seeds key the registrable site -- one scheduler, two states, diverging
circuit/backoff. Canonical key is ``NormalizedUrl.site`` (everywhere else).
"""

from collections import deque

from safe_url.normalized_url import NormalizedUrl

from crawlutron.admission.admission_links import AdmittedLink
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.orchestration.recursive import CrawlState
from tests._policy_factories import canonical


def _admit(link: DiscoveredLink) -> AdmittedLink:
    """The projection production's ``admit_discovered_links`` performs."""
    return AdmittedLink(original_url=link.url, normalized_url=link.normalized_url, depth=0)


def _link(page_url: str, source: NormalizedUrl) -> DiscoveredLink:
    return DiscoveredLink(
        url=page_url,
        normalized_url=canonical(page_url),
        text="",
        rel=[],
        source_url=source,
        host=canonical(page_url).host,
        is_same_host=True,
    )


def _empty_state(next_index: int = 0, known: set[NormalizedUrl] | None = None) -> CrawlState:
    return CrawlState(
        frontier=deque(),
        visited=set(),
        next_index=next_index,
        ramp=4.0,
        known=set() if known is None else known,
    )


def test_scheduler_domain_key_is_the_one_canonical_function():
    """One canonical key function for writers and readers: the URL's site."""
    from crawlutron.models.crawl_task import scheduler_domain_key

    url = canonical("http://WWW.Example.COM:8443/page")
    assert scheduler_domain_key(url) == url.site == "example.com"


def test_recursive_admitted_task_keys_the_scheduler_by_site():
    """#445: admitted tasks keyed 'www.example.com'; breakers key the site."""
    source = canonical("http://www.example.com/")
    link = _link("http://www.example.com/page", source)
    _depth, task = _empty_state().make_admitted_task(_admit(link))

    assert task.domain == task.normalized_url.site, (
        f"admitted task keyed scheduler by {task.domain!r}, "
        f"but the scheduler's canonical key is the registrable site "
        f"{task.normalized_url.site!r} (#445)"
    )


def test_recursive_admitted_and_seed_tasks_share_one_key_scheme():
    """Seeds and admitted links must produce the SAME key for the same site."""
    from crawlutron.models.crawl_task import CrawlTask

    seed_url = canonical("http://www.example.com/seed")
    seed_task = CrawlTask(index=0, original_url=seed_url.raw, normalized_url=seed_url)
    link = _link("http://www.example.com/page", seed_url)
    _depth, admitted = _empty_state(next_index=1, known={seed_url}).make_admitted_task(_admit(link))

    assert admitted.domain == seed_task.domain, (
        f"seed keyed {seed_task.domain!r}, admitted keyed {admitted.domain!r} -- split brain (#445)"
    )


def test_the_task_domain_can_no_longer_disagree_with_its_url():
    """#445 root cause: a caller-supplied domain field could mis-key the scheduler."""
    from dataclasses import fields

    from crawlutron.models.crawl_task import CrawlTask

    url = canonical("http://www.example.com/page")
    task = CrawlTask(index=0, original_url=url.raw, normalized_url=url)
    assert task.domain == url.site
    assert "domain" not in {f.name for f in fields(CrawlTask)}
