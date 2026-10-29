"""Cookie bootstrap: after browser renders a blocked domain, retry cffi with
the browser-minted session cookies before accepting the render result."""

from crawlutron.vocabulary.strategies import BROWSER_HEADLESS, HTTP_CFFI_IMPERSONATE, HTTP_DECLARED_CRAWLER
from tests._browser_doubles import _fake_browser
from tests._chain_policies import chain_policy
from tests._fetcher_factories import request_fetcher, run_chain

#: A browser worker's success shape, which is what the strategy chain needs back once the cffi hop has the
#: cookies.
_rendered = _fake_browser(html="<html>rendered</html>", text="rendered")


async def test_browser_cookies_replayed_through_cffi():
    from crawlutron.transport import browser_cookies

    browser_cookies.store_browser_cookies("example.com", "incap_ses=abc; visid=def")

    calls = []

    # The plain HTTP strategy always denies; the cffi one only succeeds once the browser cookies are replayed.
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


class _CookieContext:
    """A page context whose cookie jar is the test's plant."""

    def __init__(self, jar):
        self._jar = jar

    async def cookies(self, url=None):
        return self._jar


def _harvesting_worker(jar):
    """A browser worker that runs the REAL harvest before answering rendered."""
    from types import SimpleNamespace

    from crawlutron.transport.browser_cookies import harvest_session_cookies

    page = SimpleNamespace(context=_CookieContext(jar))

    async def worker(job):
        await harvest_session_cookies(page, job.url)
        return _rendered(job)

    return worker


async def test_harvested_cookies_never_cross_callers_in_the_chain():
    """Two callers crawl the same blocked domain back to back (#437).

    Caller A's browser-minted session must not ride caller B's cffi retry:
    each crawl's harvest and replay are keyed by its own cookie namespace.
    """
    from crawlutron.transport import browser_cookies
    from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE

    denied = (403, b"denied", "text/plain")
    marker = "tenant-a-session"

    def cffi_oracle(calls):
        """Unblock the hop only when it carries caller A's session marker."""

        def respond(url, headers):
            calls.append(dict(headers))
            unblocked = marker in headers.get("Cookie", "")
            return (200, b"<html><body>unblocked</body></html>", "text/html") if unblocked else denied

        return respond

    async def tenant_crawl(tenant, jar):
        """One caller's whole crawl, with the namespace crawl_request binds."""
        calls: list[dict[str, str]] = []
        policy = chain_policy(
            HTTP_DECLARED_CRAWLER,
            HTTP_CFFI_IMPERSONATE,
            BROWSER_HEADLESS,
            discovery={"robots": True},
        )
        policy["request_profile"] = {"cookie_namespace": tenant}
        token = COOKIE_NAMESPACE.set(tenant)
        try:
            result = await run_chain(
                fetcher=request_fetcher(lambda url, headers: denied),
                cffi_fetcher=request_fetcher(cffi_oracle(calls)),
                browser_worker=_harvesting_worker(jar),
                policy=policy,
            )
        finally:
            COOKIE_NAMESPACE.reset(token)
        return calls, result

    browser_cookies.reset_process_state()
    try:
        # Caller A's crawl: its own browser session unblocks its own retry.
        calls_a, result_a = await tenant_crawl("tenant-a", [{"name": "sess", "value": marker}])
        assert result_a.status == "success"
        assert any(marker in h.get("Cookie", "") for h in calls_a), calls_a

        # Caller B's crawl on the same domain: B's browser holds no session, so
        # B's retry goes out cookieless -- never carrying A's session.
        calls_b, result_b = await tenant_crawl("tenant-b", [])
        leaked = [h for h in calls_b if marker in h.get("Cookie", "")]
        assert not leaked, f"caller B's retry carried caller A's session: {leaked!r}"
        assert result_b.classification == "rendered", result_b
    finally:
        browser_cookies.reset_process_state()
