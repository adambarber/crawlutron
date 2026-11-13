"""Residual cookie-partitioning bypasses (#483).

The namespace law: a keyed crawl's browser harvest, its redirect hops, and its
retry all key by that crawl's namespace, so cross-namespace reads return nothing.
"""

from tests._policy_factories import canonical


def _planted_jar(namespace: str):
    """The process jar holding one hop cookie in ``namespace``; state reset around."""
    from crawlutron.runtime import process_state
    from crawlutron.transport.cookie_jar import global_cookie_jar

    process_state.reset_all_process_state()
    jar = global_cookie_jar()
    jar.add_cookie_header(canonical("http://b.example/"), "hop=1; Path=/", namespace=namespace)
    return jar


def _hop_under(namespace: str):
    """One redirect hop with ``namespace`` bound; the (url, headers) result."""
    from crawlutron.runtime import process_state
    from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE

    _planted_jar(namespace)
    token = COOKIE_NAMESPACE.set(namespace)
    try:
        result = _run_redirect({"Cookie": "caller=private"})
    finally:
        COOKIE_NAMESPACE.reset(token)
    process_state.reset_all_process_state()
    return result


def _run_redirect(headers):
    import asyncio

    from crawlutron.transport.pinned_transport import redirect_target

    return asyncio.run(
        redirect_target(canonical("http://a.example/x"), headers, 301, "http://b.example/y", 0)
    )


def test_redirect_reattach_reads_the_crawl_namespace():
    """A redirect hop re-attaches only the crawl namespace's jar cookies (#483).

    The hop must not fall back to the shared default when a namespace is bound:
    a keyed caller's other-origin jar read keys by COOKIE_NAMESPACE, not ``""``.
    """
    result = _hop_under("tenant-a")
    # The hop into b.example carries tenant-a's own jar cookie, not nothing.
    assert result is not None and "hop=1" in result[1].get("Cookie", "")


def test_redirect_reattach_shared_namespace_reads_shared_slot():
    """The un-namespaced crawl keeps the legacy shared slot (behavior unchanged)."""
    result = _hop_under("")
    assert result is not None and "hop=1" in result[1].get("Cookie", "")


def test_browser_harvest_keys_by_the_crawl_namespace():
    """The render harvest stores into the crawl's namespace, not always ``""`` (#483).

    A keyed crawl's harvested session must never surface to an anonymous or
    other-tenant read: the store and the get are keyed by the same namespace.
    """
    from crawlutron.runtime import process_state
    from crawlutron.transport import browser_cookies

    process_state.reset_all_process_state()
    try:
        page = _page_with_cookies([{"name": "sess", "value": "a-secret"}])
        _run_harvest(page, namespace="tenant-a")
        # Same namespace reads its own harvest...
        assert browser_cookies.get_browser_cookies_for("example.com", namespace="tenant-a") == "sess=a-secret"
        # ...another namespace reads nothing...
        assert browser_cookies.get_browser_cookies_for("example.com", namespace="tenant-b") is None
        # ...and the shared default reads nothing either.
        assert browser_cookies.get_browser_cookies_for("example.com", namespace="") is None
    finally:
        process_state.reset_all_process_state()


def test_browser_harvest_defaults_to_the_bound_contextvar_namespace():
    """``namespace=None`` defers to the crawl's bound COOKIE_NAMESPACE (#483).

    release_page calls the harvest without an explicit namespace, so the default
    must be the bound crawl namespace -- not a hardcoded shared slot."""
    from crawlutron.runtime import process_state
    from crawlutron.transport import browser_cookies
    from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE

    process_state.reset_all_process_state()
    try:
        page = _page_with_cookies([{"name": "sess", "value": "bound"}])
        token = COOKIE_NAMESPACE.set("tenant-a")
        try:
            _run_harvest(page)
        finally:
            COOKIE_NAMESPACE.reset(token)
        assert browser_cookies.get_browser_cookies_for("example.com", namespace="tenant-a") == "sess=bound"
        assert browser_cookies.get_browser_cookies_for("example.com", namespace="") is None
    finally:
        process_state.reset_all_process_state()


def _page_with_cookies(cookies):
    from types import SimpleNamespace

    class _Context:
        async def cookies(self, url=None):
            return cookies

    return SimpleNamespace(context=_Context())


def _run_harvest(page, *, namespace=None):
    import asyncio

    from safe_url.host_policy import HostPolicy
    from safe_url.normalized_url import NormalizedUrl

    from crawlutron.transport.browser_cookies import harvest_session_cookies

    url = NormalizedUrl("http://example.com/", policy=HostPolicy.unrestricted())

    async def _go():
        await harvest_session_cookies(page, url, namespace=namespace)

    asyncio.run(_go())
