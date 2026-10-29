"""The shared cookie store never runs on the event loop (#141, #5833).

With persistence on the jar is SQLite-backed; the async path crosses it for
the Cookie-header read, the Set-Cookie store, and the redirect re-attach.
"""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.transport import cookie_jar
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests.api._offload_doubles import LoopGuardSharedCookieStore


def _refuse_on_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError(f"sync cookie-store method {name} ran on the event loop")


# LoopGuardSharedCookieStore lives in tests/api/_offload_doubles.py with the
# other loop-guard store doubles; the redirect door's test imports it there.


def _url(path: str = "https://example.test/a") -> NormalizedUrl:
    return NormalizedUrl(path, policy=UNRESTRICTED_HOST_POLICY)


def _policy_with_cookie_jar():
    """A policy whose profile asks for the persistent jar, so headers read it."""
    from tests._policy_factories import http_strategy_policy

    policy = http_strategy_policy()
    policy.request_profile = {"cookie_jar": "per_host"}
    return policy


@pytest.fixture
def guarded_jar(tmp_path):
    """Install a loop-guarded jar, and restore the jar object it replaced.

    The fixture made the swap, so the fixture owns undoing it (belt vs the door).
    """
    previous = cookie_jar.global_cookie_jar()
    jar = cookie_jar.CookieJar(LoopGuardSharedCookieStore(tmp_path / "cookies.db"))
    cookie_jar.set_global_cookie_jar(jar)
    yield jar
    cookie_jar.set_global_cookie_jar(previous)
    cookie_jar.reset_process_state()


async def test_storing_cookies_from_the_async_fetch_path_stays_off_the_loop(guarded_jar):
    """The fetch tails' Set-Cookie door is awaited from async frames; a sync
    store.merge there is a SQLite write on the loop."""
    from crawlutron.transport.request_profile import store_set_cookies

    class _Headers:
        def get_list(self, key: str) -> list[str]:
            return ["session=abc; Path=/; HttpOnly"] if key == "set-cookie" else []

        def get(self, key: str, default: str = "") -> str:
            return default

        def items(self):
            return [("Set-Cookie", "session=abc; Path=/; HttpOnly")]

    await store_set_cookies(_url(), _Headers(), namespace="")
    stored = await asyncio.to_thread(guarded_jar.store.load, "", "example.test")
    assert "session" in stored and '"value": "abc"' in stored["session"]


async def test_building_request_headers_from_the_async_frame_stays_off_the_loop(
    guarded_jar,
):
    """``resolve_http_limits`` runs in the async HTTP strategy frame; its jar
    read (the Cookie header) is a SQLite read there, so the resolver awaits it
    off the loop."""
    from crawlutron.transport.http_cache import resolve_http_limits
    from crawlutron.transport.http_response import HttpStrategyContext

    jar = cookie_jar.global_cookie_jar()
    await jar.aadd_cookie_header(_url(), "session=abc; Path=/", namespace="")
    ctx = HttpStrategyContext(
        original_url="https://example.test/a",
        normalized_url=_url(),
        strategy="HTTP_DECLARED_CRAWLER",
        policy=_policy_with_cookie_jar(),
        host_policy=UNRESTRICTED_HOST_POLICY,
        http_fetcher=None,
        cache=None,
        content_cache=None,
        caller_validators={},
        robots_sitemaps=[],
        global_concurrency=2,
        started=0.0,
    )
    await resolve_http_limits(ctx)
    assert ctx.base_headers.get("Cookie") == "session=abc"


async def test_a_cross_origin_redirect_reattach_stays_off_the_loop(monkeypatch, guarded_jar):
    """The redirect hop's cookie re-attach must not run the SQLite store on the
    loop. The fetcher's own redirect loop is driven for real -- a scripted 302
    to another origin -- and the store itself refuses the loop.
    """
    from tests._fetcher_factories import run_default_http_fetcher
    from tests._transport_doubles import MockResponse, patch_http_fetcher_transport

    jar = cookie_jar.global_cookie_jar()
    # Seed the jar off the loop through its derived twin: the cookie the new
    # origin's re-attach would look for, so the read path really runs.
    await jar.aadd_cookie_header(_url(), "session=abc; Path=/", namespace="")
    client = patch_http_fetcher_transport(
        monkeypatch,
        [
            MockResponse(302, {"location": "http://other.example/steal"}),
            MockResponse(200, {"content-type": "text/html"}, b"<html>ok</html>"),
        ],
        allow_ssrf_bypass=True,
    )
    result = await run_default_http_fetcher("http://example.test/", {"Cookie": "caller=1"})
    assert result.status_code == 200, "the redirected fetch must complete for the verdict to count"
    # The re-attach attached the NEW origin's own jar cookie (none here: the
    # seed is example.test's), and the caller's credential never crossed over.
    second_hop = client.captured_headers[1]
    assert second_hop.get("Cookie") is None, second_hop
