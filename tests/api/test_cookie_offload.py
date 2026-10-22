"""The shared cookie store never runs on the event loop (#141, #5833).

With persistence on the jar is SQLite-backed; the async path crosses it for
the Cookie-header read and the Set-Cookie store. The guard: the real store.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.normalized_url import NormalizedUrl
from app.storage.coordination_cookies import SharedCookieStore
from app.transport import cookie_jar
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


class LoopGuardSharedCookieStore(SharedCookieStore):
    """The real cookie store, every sync entry point refusing the loop."""

    def load(self, namespace: str, host: str) -> dict[str, str]:
        _refuse_on_loop("SharedCookieStore.load")
        return super().load(namespace, host)

    def merge(self, namespace: str, host: str, cookies) -> None:
        _refuse_on_loop("SharedCookieStore.merge")
        return super().merge(namespace, host, cookies)

    def touch(self, namespace: str, host: str) -> None:
        _refuse_on_loop("SharedCookieStore.touch")
        return super().touch(namespace, host)

    def all_slots(self):
        _refuse_on_loop("SharedCookieStore.all_slots")
        return super().all_slots()

    def clear(self, namespace: str = "") -> None:
        _refuse_on_loop("SharedCookieStore.clear")
        return super().clear(namespace)


def _refuse_on_loop(name: str) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError(f"sync cookie-store method {name} ran on the event loop")


def _url(path: str = "https://example.test/a") -> NormalizedUrl:
    return NormalizedUrl(path, policy=UNRESTRICTED_HOST_POLICY)


def _policy_with_cookie_jar():
    """A policy whose profile asks for the persistent jar, so headers read it."""
    from tests._policy_factories import http_strategy_policy

    policy = http_strategy_policy()
    policy.request_profile = {"cookie_jar": "per_host"}  # type: ignore[union-attr]
    return policy


@pytest.fixture
def guarded_jar(tmp_path):
    jar = cookie_jar.CookieJar(LoopGuardSharedCookieStore(tmp_path / "cookies.db"))
    cookie_jar.set_global_cookie_jar(jar)
    yield jar
    cookie_jar.reset_process_state()


async def test_storing_cookies_from_the_async_fetch_path_stays_off_the_loop(guarded_jar):
    """The fetch tails' Set-Cookie door is awaited from async frames; a sync
    store.merge there is a SQLite write on the loop."""
    from app.transport.request_profile import store_set_cookies

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
    from app.transport.http_cache import resolve_http_limits
    from app.transport.http_response import HttpStrategyContext

    jar = cookie_jar.global_cookie_jar()
    await jar.aadd_cookie_header(_url(), "session=abc; Path=/", namespace="")
    ctx = HttpStrategyContext(
        original_url="https://example.test/a",
        normalized_url=_url(),
        strategy="http",
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
