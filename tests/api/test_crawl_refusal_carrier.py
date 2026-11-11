"""The executor's refusal carrier, at its own seam.

The sweep's deactivation decision reads the reason off the exception; these
pins close the carrier's contract: every constructor refusal survives as data,
and foreign text degrades to the named fallback instead of entering the enum.
"""

from __future__ import annotations

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_strategy import CrawlStrategy
from crawlutron.service.crawl_executor import CrawlRefusalError
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER

SETTINGS = CrawlerSettings.from_mapping({"CRAWLUTRON_DENIED_HOSTS": "blocked.test"})


def _request(url: str) -> CrawlRequest:
    return CrawlRequest(url=url, policy=CrawlPolicy(strategies=[CrawlStrategy(HTTP_DECLARED_CRAWLER)]))


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://blocked.test/page", "host_denied"),
        ("ftp://blocked.test/page", "unsupported_scheme"),
        ("http://user:pass@blocked.test/", "userinfo_refused"),
        ("not a url", "unsupported_scheme"),
    ],
)
async def test_every_constructor_refusal_crosses_as_a_typed_member(url: str, reason: str):
    """The executor re-raises the constructor's own reason in the typed carrier:
    the sweep reads a member, never a bare string cast at the boundary."""
    with pytest.raises(CrawlRefusalError) as raised:
        await CrawlRefusalError_from_executor(url)
    assert raised.value.reason.value == reason


async def CrawlRefusalError_from_executor(url: str) -> None:
    """Drive the executor's own admission edge (the wrapper's real signature)."""
    from crawlutron.service.crawl_executor import execute_crawl

    await execute_crawl(_request(url), settings=SETTINGS, shutdown_event=None)


def test_a_foreign_reason_degrades_to_the_named_fallback():
    """Foreign text from any future raise site never enters the strict enum: it
    deactivates rows under unknown_error, the named fallback, instead."""
    error = CrawlRefusalError("some_future_refusal_reason")
    assert error.reason.value == "unknown_error"
