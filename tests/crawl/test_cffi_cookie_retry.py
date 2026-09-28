"""Cookie bootstrap: after browser renders a blocked domain, retry cffi with
the browser-minted session cookies before accepting the render result."""

from tests._browser_doubles import _fake_browser
from _chain_policies import chain_policy
from tests._fetcher_factories import request_fetcher, run_chain

from app.constants import BROWSER_HEADLESS, HTTP_CFFI_IMPERSONATE, HTTP_DECLARED_CRAWLER

#: A browser worker's success shape, which is what the strategy chain needs
#: back once the cffi hop has the cookies.
_rendered = _fake_browser(html="<html>rendered</html>", text="rendered")


async def test_browser_cookies_replayed_through_cffi(monkeypatch):
    from app.crawl.strategies import browser as bmod

    monkeypatch.setitem(bmod._BROWSER_COOKIES_BY_DOMAIN, "example.com", "incap_ses=abc; visid=def")

    calls = []

    # The plain HTTP strategy always denies; the cffi one only succeeds once
    # the browser cookies are replayed. Both answers are functions of the
    # Cookie header, so both are request_fetcher responses -- and the tag each
    # call is recorded under is what the test reads back.
    denied = (403, b"denied", "text/plain")

    def respond_http(url, headers):
        calls.append(("http", dict(headers)))
        return denied

    def respond_cffi(url, headers):
        calls.append(("cffi", dict(headers)))
        if "incap_ses" in headers.get("Cookie", ""):
            return (200, b"<html><body>cffi success</body></html>", "text/html")
        return denied

    http_fetcher = request_fetcher(respond_http)
    cffi_fetcher = request_fetcher(respond_cffi)

    async def browser_worker(job):
        return _rendered(job)

    result = await run_chain(
        fetcher=http_fetcher,
        cffi_fetcher=cffi_fetcher,
        browser_worker=browser_worker,
        policy=chain_policy(
            HTTP_DECLARED_CRAWLER,
            HTTP_CFFI_IMPERSONATE,
            BROWSER_HEADLESS,
            discovery={"robots": True},
        ),
    )
    assert result.status == "success"
    assert result.diagnostics["robots"]["evaluation"] == "disabled"
    # final cffi attempt must carry the browser cookies
    cffi_calls = [h for kind, h in calls if kind == "cffi"]
    assert any("incap_ses=abc" in h.get("Cookie", "") for h in cffi_calls), calls
