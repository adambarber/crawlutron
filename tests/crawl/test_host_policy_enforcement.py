from __future__ import annotations

import httpx
import pytest

import app.crawl.strategies.fetchers as fetchers_mod
import app.crawl.strategies.http_client_pool as pool_mod
import app.crawl.strategies.pinned_transport as pinned_mod
from app.crawl.engine import crawl_request
from app.crawl.extraction import extract_post_fetch_discovery
from app.settings import CrawlerSettings
from app.validation import HostPolicy
from tests._browser_doubles import _fake_browser
from tests._fetcher_factories import make_html_fetcher, routed_fetcher


def _settings(*, allowed: str = "", denied: str = "") -> CrawlerSettings:
    return CrawlerSettings.from_mapping(
        {
            "BROWSER_CRAWLER_ALLOWED_HOSTS": allowed,
            "BROWSER_CRAWLER_DENIED_HOSTS": denied,
        }
    )


#: One link deep, ten pages, no robots: the shallowest policy that still
#: exercises discovery, which is the half of these crawls under test.
ONE_HOP_LIMITS = {"max_depth": 1, "max_pages": 10, "respect_robots": False}


def _one_hop_policy(**extra) -> dict:
    """A one-hop crawl policy, fresh per call because callers may mutate it."""
    return {"limits": dict(ONE_HOP_LIMITS), **extra}


@pytest.mark.parametrize(
    ("allowed", "denied"),
    [
        ("allowed.test", ""),
        ("", "blocked.test"),
        ("allowed.test", "blocked.test"),
    ],
)
async def test_recursive_crawl_never_fetches_policy_blocked_discovered_host(allowed, denied):
    calls: list[str] = []

    fetcher = make_html_fetcher(
        calls,
        b'<html><body><a href="https://blocked.test/secret">blocked</a></body></html>',
    )

    response = await crawl_request(
        {"url": "https://allowed.test/", "policy": _one_hop_policy()},
        settings=_settings(allowed=allowed, denied=denied),
        http_fetcher=fetcher,
    )

    assert response["results"][0]["status"] == "success"
    assert calls == ["https://allowed.test"]


async def test_sitemap_blocked_host_is_refused_before_page_fetch():
    calls: list[str] = []

    fetcher = routed_fetcher(
        {
            "/sitemap.xml": (
                200,
                b"<urlset><url><loc>https://allowed.test/keep</loc></url>"
                b"<url><loc>https://blocked.test/secret</loc></url></urlset>",
                "application/xml",
            ),
        },
        calls=calls,
        default=(200, b"<html>ok</html>", "text/html"),
    )

    response = await crawl_request(
        {"url": "https://allowed.test/", "policy": _one_hop_policy(discovery={"sitemaps": True})},
        settings=_settings(allowed="allowed.test", denied="blocked.test"),
        http_fetcher=fetcher,
    )

    crawled = {result["normalized_url"] for result in response["results"]}
    assert "https://allowed.test/keep" in crawled
    assert not any(url.startswith("https://blocked.test/") for url in calls)


class _Response:
    def __init__(self, status, headers, url):
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self.url = url

    async def aiter_raw(self):
        if False:
            yield b""


class _Stream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class _Client:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, bool]] = []

    def stream(self, method, url, headers=None, timeout=None, follow_redirects=False):
        self.calls.append((url, follow_redirects))
        return _Stream(self.response)


async def test_redirect_to_policy_blocked_host_is_refused_before_fetch(monkeypatch):
    client = _Client(
        _Response(
            302,
            {"location": "https://blocked.test/secret"},
            "https://allowed.test/",
        )
    )
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: client)
    monkeypatch.setattr(pinned_mod, "_check_ssrf_protection", lambda url: None)
    monkeypatch.setattr(pinned_mod, "_resolve_and_pin_host", lambda url: ("93.184.216.34", ""))

    with pytest.raises(ValueError, match="host_denied"):
        await fetchers_mod._default_http_fetcher(
            "https://allowed.test/",
            {},
            30,
            policy=HostPolicy.from_strings("allowed.test", "blocked.test"),
        )

    assert [url for url, _follow in client.calls] == ["https://allowed.test"]
    assert all(follow is False for _url, follow in client.calls)


async def test_render_request_never_invokes_worker_for_policy_blocked_host():
    calls: list[str] = []
    rendered = _fake_browser(html="<html></html>", text="rendered")

    async def worker(job):
        calls.append(job.url)
        return rendered(job)

    from app.api.render import render_request

    with pytest.raises(ValueError, match="host_denied"):
        await render_request(
            {"url": "https://blocked.test/secret"},
            settings=_settings(denied="blocked.test"),
            worker=worker,
        )

    assert calls == []


def test_js_endpoint_extraction_filters_private_targets_on_real_discovery_path(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    policy = HostPolicy.from_strings("", "")
    html = "<script>fetch('/api/public'); fetch('http://127.0.0.1/private');</script>"

    discovered = extract_post_fetch_discovery(
        html,
        "https://allowed.test/page",
        include_js_endpoints=True,
        host_policy=policy,
    )

    assert discovered["js_endpoints"] == [{"url": "https://allowed.test/api/public"}]


async def test_allow_listed_private_target_is_still_refused(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    calls: list[str] = []

    fetcher = make_html_fetcher(calls, b"<html></html>")

    with pytest.raises(ValueError, match="private_network_target"):
        await crawl_request(
            {"url": "http://127.0.0.1/private"},
            settings=_settings(allowed="127.0.0.1"),
            http_fetcher=fetcher,
        )

    assert calls == []
