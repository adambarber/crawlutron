"""The entry path builds each NormalizedUrl once and keeps the value (#351)."""

import asyncio
from collections.abc import Callable

import pytest
from safe_url.host_policy import HostPolicy
from safe_url.normalized_url import NormalizedUrl

from crawlutron.admission.payload_helpers import checked_payload_urls
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.orchestration.request_pipeline import PipelineDependencies, PipelineState, build_jobs
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from tests._fetch_doubles import FetcherDouble


def _idle_gate() -> ResourceGate:
    """A gate reporting no pressure, the admission-neutral shape."""
    return ResourceGate(memory_ratio=lambda: 0.0, cpu_ratio=lambda: 0.0)


POLICY = HostPolicy.unrestricted()


class TestCheckedPayloadUrlPairs:
    def test_pairs_carry_original_text_and_normalized_value(self) -> None:
        pairs = checked_payload_urls(CrawlRequest(url="http://x/"), 10, host_policy=POLICY)
        assert [text for text, _ in pairs] == ["http://x/"]
        assert isinstance(pairs[0][1], NormalizedUrl)

    def test_pairs_dedupe_on_admitted_identity_keeping_first_text(self) -> None:
        urls = [
            "https://a.gov/x",
            "https://a.gov/x/#section",
            "https://a.gov/x/?utm_source=tw",
            "https://a.gov/y?a=1&b=2",
            "https://a.gov/y?b=2&a=1",
        ]
        pairs = checked_payload_urls(CrawlRequest(urls=urls), 10, host_policy=POLICY)
        assert [text for text, _ in pairs] == [
            "https://a.gov/x",
            "https://a.gov/x/#section",
            "https://a.gov/y?a=1&b=2",
        ]

    def test_pairs_reject_invalid_url_and_enforce_cap(self) -> None:
        with pytest.raises(ValueError, match=r"^invalid_url$"):
            checked_payload_urls(CrawlRequest(url="ftp://x/"), 10, host_policy=POLICY)
        with pytest.raises(ValueError, match=r"^batch_size_exceeded$"):
            checked_payload_urls(CrawlRequest(urls=["http://x/", "http://y/"]), 1, host_policy=POLICY)

    def test_forwarded_pairs_are_shape_checked_capped_but_not_deduped(self) -> None:
        forwarded: list[tuple[str, NormalizedUrl]] = [
            ("http://x/", NormalizedUrl("http://x/", policy=POLICY))
        ] * 2
        pairs = checked_payload_urls(
            CrawlRequest(url="http://z/"), 10, host_policy=POLICY, url_pairs=forwarded
        )
        assert pairs == forwarded
        # A deliberately malformed pair is a TypeError, not a silent admit; an
        # object mapping is the honest spelling of "wrong pair shape".
        bad_pairs: object = [("http://x/", "not-a-url")]
        bad_checked: Callable[..., object] = checked_payload_urls
        with pytest.raises(ValueError, match=r"^invalid_url$"):
            bad_checked(CrawlRequest(url="http://z/"), 10, host_policy=POLICY, url_pairs=bad_pairs)

    def test_crawl_request_consumes_forwarded_pairs(self) -> None:
        """The engine accepts the gate's pairs directly (#351 seam)."""
        forwarded = [("http://x/", NormalizedUrl("http://x/", policy=POLICY))]
        pairs = checked_payload_urls(
            CrawlRequest(url="http://z/"), 10, host_policy=POLICY, url_pairs=forwarded
        )
        assert pairs is forwarded or pairs == forwarded


class TestBuildJobsConsumesPairs:
    def test_jobs_reuse_the_admitted_normalized_url_object(self) -> None:
        """build_jobs consumes the gate's pairs; it never constructs a URL again."""
        unrestricted = HostPolicy.unrestricted()
        url_pairs = [
            ("http://x/", NormalizedUrl("http://x/", policy=unrestricted)),
            ("http://y/", NormalizedUrl("http://y/", policy=unrestricted)),
        ]
        state = PipelineState(
            payload=CrawlRequest(url="http://x/"),
            settings=CrawlerSettings.from_mapping(),
            url_pairs=url_pairs,
            policy=CrawlPolicy(),
            fetcher=FetcherDouble(lambda *_: (200, b"", "text/html")),  # never awaited
            cffi=FetcherDouble(lambda *_: (200, b"", "text/html")),  # never awaited
            host_policy=unrestricted,
            shutdown_event=asyncio.Event(),
            browser_worker=None,
            cache=None,
            content_cache=None,
            webhook_url=None,
            spill_store=CrawlSpillStore(":memory:"),
            # build_jobs reads only url_pairs; the defaults never fire here, so
            # the bag is built through a lambda distinct from any other file's.
            dependencies=PipelineDependencies(
                get_content_cache=lambda: ContentCache(url_pairs and "" or ""),
                compile_scope_regex_safe=lambda compiled: None,
                fresh_seeds=lambda seed, cache, policy: set(),
                fetch_and_decode=None,
                shared_http_cache=HttpCache(),
                resource_gate=_idle_gate(),  # nothing fires
            ),
        )
        jobs = build_jobs(state)
        assert [job.original_url for job in jobs] == ["http://x/", "http://y/"]
        assert [job.normalized_url for job in jobs] == [pair[1] for pair in url_pairs]
        assert jobs[0].normalized_url is url_pairs[0][1]
