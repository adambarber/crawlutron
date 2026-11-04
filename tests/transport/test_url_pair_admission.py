"""The entry path builds each NormalizedUrl once and keeps the value (#351)."""

import pytest

from crawlutron.admission.payload_helpers import checked_payload_urls
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.orchestration.request_pipeline import build_jobs

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
        forwarded = [("http://x/", NormalizedUrl("http://x/", policy=POLICY))] * 2
        pairs = checked_payload_urls(
            CrawlRequest(url="http://z/"), 10, host_policy=POLICY, url_pairs=forwarded
        )
        assert pairs == forwarded
        with pytest.raises(ValueError, match=r"^invalid_url$"):
            checked_payload_urls(
                CrawlRequest(url="http://z/"), 10, host_policy=POLICY, url_pairs=[("http://x/", "not-a-url")]
            )

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

        class _State:
            url_pairs = [
                ("http://x/", NormalizedUrl("http://x/", policy=HostPolicy.unrestricted())),
                ("http://y/", NormalizedUrl("http://y/", policy=HostPolicy.unrestricted())),
            ]
            host_policy = HostPolicy.unrestricted()

        jobs = build_jobs(_State())
        assert [job.original_url for job in jobs] == ["http://x/", "http://y/"]
        assert [job.normalized_url for job in jobs] == [pair[1] for pair in _State.url_pairs]
        assert jobs[0].normalized_url is _State.url_pairs[0][1]
