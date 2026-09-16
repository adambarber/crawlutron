"""C2: the long-lived clients must not keep their own cookie jar.

The shared cffi ``AsyncSession`` (and the shared httpx ``AsyncClient``) each ship
a requests/httpx-compatible cookie jar. Left active, that jar stores every
Set-Cookie and replays domain-matching cookies on later requests INDEPENDENTLY
of the namespaced ``_GLOBAL_COOKIE_JAR``. The module's threat model states that
multi-tenant deployments set ``request_profile.cookie_namespace`` precisely so
"concurrent crawls from different tenants cannot leak cookies"; a client jar
that replays by domain silently defeats that on every request it serves, and can
even clobber the namespaced ``Cookie:`` header the fetcher set.

The fix is at the construction chokepoint, not the call sites: cookies must flow
only through the namespaced jar, so each client's own jar is inert from birth.
"""

import pytest


@pytest.mark.real_cffi
@pytest.mark.usefixtures("real_sleeps")
async def test_cffi_session_does_not_replay_cookies_it_received(base_url):
    """The impersonated session's own jar must be inert (store nothing, replay
    nothing); a cookie it saw on one request must never come back on the next.

    Two requests to the same domain is the whole leak: tenant A's Set-Cookie
    becomes tenant B's ``Cookie:`` header when the jar replays by domain.
    """
    pytest.importorskip("curl_cffi")
    from app.crawl.strategies.http import _build_cffi_session

    session = _build_cffi_session()
    try:
        await session.get(base_url + "/set-cookie")
        replayed = await session.get(base_url + "/check-cookie")
    finally:
        await session.close()

    assert "session_id" not in replayed.text, (
        f"the cffi session replayed a cookie its own jar absorbed: {replayed.text!r}"
    )


async def test_shared_httpx_client_does_not_replay_cookies_it_received(base_url):
    """The long-lived httpx client shares the defect class, so the same chokepoint.

    ``get_async_http_client`` serves every declared-crawler fetch and outlives
    them all, so its jar accumulates across tenants for the life of the process.
    """
    from app.crawl.strategies.http import _build_async_client

    client = _build_async_client(None)
    try:
        await client.get(base_url + "/set-cookie")
        replayed = await client.get(base_url + "/check-cookie")
    finally:
        await client.aclose()

    assert "session_id" not in replayed.text, (
        f"the shared httpx client replayed a cookie its own jar absorbed: {replayed.text!r}"
    )
