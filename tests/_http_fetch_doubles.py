"""Shared HTTP fetch doubles for the transport edge tests.

Declared support surface (#476): moved verbatim from test_http_helpers.py so a
sibling test module imports these from here, not from another test's internals.
"""

from __future__ import annotations

from crawlutron.admission.policy import read_policy
from crawlutron.transport.http import http_strategy_result
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._fetch_doubles import scripted_fetcher
from tests._fetch_scripted import routed_fetcher
from tests._fetcher_factories import (
    make_fetch,
    strategy_request,
)
from tests._policy_factories import make_base_policy

_DISCOVERY_ALL_ON = {"robots": True, "sitemaps": True, "feeds": True}

__all__ = ["FEED_ADVERTISING_PAGE", "diagnostics_for_failed_probes", "fake_fetcher"]

FEED_ADVERTISING_PAGE = (
    200,
    b'<html><body><link rel="alternate" type="application/rss+xml"'
    b' href="http://example.com/feed.xml"></body></html>',
    "text/html",
)


def fake_fetcher(calls):
    return routed_fetcher(
        {
            "/robots.txt": (200, "User-agent: *\nDisallow: /secret\n", "text/plain"),
            "sitemap": (
                200,
                "<urlset><loc>https://x.test/p1</loc><lastmod>2024-05-05</lastmod></urlset>",
                "application/xml",
            ),
        },
        calls=calls,
        default=(200, b"<html></html>", "text/html"),
    )


# The URL the default single-page drive fetches, and therefore the one a caller has to differ from for
# ``final_url`` to mean anything.
_PAGE_URL = "http://example.com/"


async def run_http_result(
    status=200,
    strategy=HTTP_DECLARED_CRAWLER,
    fetcher=None,
    ctype="text/html; charset=utf-8",
    body=b"<html><body>hello</body></html>",
    final_url=_PAGE_URL,
    policy=None,
):
    """Drive the real ``http_strategy_result`` over a default single-page policy.

    ``final_url`` is honoured now -- it used to be accepted and dropped.
    """
    if fetcher is None:
        fetcher = (
            scripted_fetcher(status=status, ctype=ctype, body=body)
            if final_url == _PAGE_URL
            else routed_fetcher(
                default=make_fetch(status=status, body=body, ctype=ctype, final_url=final_url),
            )
        )

    # A policy partial merges as a raw dict and is read once, the way the wire merges it.
    base = make_base_policy()
    pol = read_policy({**base.model_dump(), **policy}) if policy else base
    return await http_strategy_result(
        request=strategy_request("http://example.com/", pol, strategy),
        http_fetcher=fetcher,
    )


async def diagnostics_for_failed_probes(fetcher, **policy):
    """Full discovery whose probes fail: the ``(result, diagnostics)`` pair.

    Discovery probes fail; the page fetch does not. The point is a failed probe
    is recorded as diagnostics and does not fail the crawl.
    """
    result = await run_http_result(
        fetcher=fetcher,
        policy={
            "limits": {"timeout_seconds": 5, "max_retries": 0},
            "discovery": _DISCOVERY_ALL_ON,
            **policy,
        },
    )
    assert result.status == "success"
    assert "robots" in result.diagnostics
    return result, result.diagnostics
