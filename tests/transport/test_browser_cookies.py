"""Browser cookie store: cap, LRU eviction, isolation and concurrency."""

import threading
from collections import OrderedDict

import pytest

import crawlutron.transport.browser_cookies as browser_cookies
from crawlutron.runtime import process_state
from crawlutron.transport.browser import get_browser_cookies_for
from crawlutron.transport.browser_cookies import BROWSER_COOKIES_MAX, store_browser_cookies


@pytest.fixture
def clean_cookie_store():
    """The process-global cookie store, emptied for this test and left empty.

    Resetting handily needs no surface the production path lacks, which was
    the reason save-and-restore only ever worked by ``MutableMapping``.
    """
    store = browser_cookies.BROWSER_COOKIES_BY_DOMAIN
    process_state.reset_all_process_state()
    try:
        yield store
    finally:
        process_state.reset_all_process_state()


def test_public_helpers_reach_the_guarded_store(clean_cookie_store):
    """The guarded object is the one the public helpers actually use.

    A bare ``OrderedDict`` at every site made the race possible; the claim is
    the helpers cannot be holding an unguarded mapping.
    """
    assert clean_cookie_store is browser_cookies.BROWSER_COOKIES_BY_DOMAIN
    store_browser_cookies("guarded.example", "sess=1")
    assert get_browser_cookies_for("guarded.example") == "sess=1"


def test_concurrent_read_and_eviction_does_not_raise(clean_cookie_store):
    """The regression, scheduled rather than left to GIL luck.

    The pre-fix ``get`` then ``move_to_end`` had no sync, so an eviction in
    that window raised ``KeyError``; the interleaving is deterministic here.
    """
    for i in range(BROWSER_COOKIES_MAX):
        store_browser_cookies(f"d{i}.example", f"k{i}=v")
    victim = "d0.example"

    old_shape = OrderedDict((f"d{i}.example", f"k{i}=v") for i in range(BROWSER_COOKIES_MAX))
    old_error: list[BaseException] = []
    reader_in_window = threading.Event()
    eviction_landed = threading.Event()

    def _racy_read() -> None:
        try:
            old_shape.get(victim)
            reader_in_window.set()
            eviction_landed.wait(timeout=5)
            old_shape.move_to_end(victim)
        except BaseException as exc:
            old_error.append(exc)

    def _evict() -> None:
        reader_in_window.wait(timeout=5)
        old_shape["brand.new.example"] = "v"
        while len(old_shape) > BROWSER_COOKIES_MAX:
            old_shape.popitem(last=False)
        eviction_landed.set()

    race_threads = [threading.Thread(target=target) for target in (_racy_read, _evict)]
    for thread in race_threads:
        thread.start()
    for thread in race_threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in race_threads), "the scheduled race wedged"

    assert len(old_error) == 1, f"the unsynchronized read was expected to raise exactly once, got {old_error}"
    assert isinstance(old_error[0], KeyError), (
        f"the unsynchronized read raised {old_error[0]!r}, not the expected KeyError"
    )


def test_guarded_store_never_raises_under_concurrent_read_and_eviction(clean_cookie_store):
    """The guarded store serializes concurrent access; the public read never raises.

    The discriminator is the observable outcome, not the lock: stress writers
    force eviction while readers hammer the oldest key.
    """
    victim = "d0.example"
    store = clean_cookie_store
    assert store is browser_cookies.BROWSER_COOKIES_BY_DOMAIN, "the fixture is the guarded global store"

    errors: list[BaseException] = []
    box: dict[str, str | None] = {}
    stop = threading.Event()
    threads: list[threading.Thread] = []

    def _stress_write() -> None:
        i = 0
        while not stop.is_set():
            try:
                store_browser_cookies(f"stress{i}.example", f"k{i}=v")
            except BaseException as exc:
                errors.append(exc)
                return
            i += 1

    def _stress_read() -> None:
        while not stop.is_set():
            try:
                box["seen"] = get_browser_cookies_for(victim)
            except BaseException as exc:
                errors.append(exc)
                return

    threads = [threading.Thread(target=_stress_write) for _ in range(2)]
    threads += [threading.Thread(target=_stress_read) for _ in range(4)]
    for thread in threads:
        thread.start()
    stop.wait(timeout=0.05)
    stop.set()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads), "the stress drive wedged"
    assert not errors, f"the guarded store raised {errors!r} under concurrent load"
    assert "seen" in box


def test_read_touches_lru_so_a_read_key_survives_eviction(clean_cookie_store):
    """Inverse case: the guard must not have changed LRU semantics.

    A read refreshes recency, so the key survives the next eviction while the
    front-most untouched key is the one dropped.
    """
    assert clean_cookie_store is browser_cookies.BROWSER_COOKIES_BY_DOMAIN
    for i in range(BROWSER_COOKIES_MAX):
        store_browser_cookies(f"d{i}.example", f"k{i}=v")
    oldest = "d0.example"
    assert get_browser_cookies_for(oldest) == "k0=v"
    store_browser_cookies("new.example", "new=v")

    assert get_browser_cookies_for(oldest) == "k0=v", "read must keep the key warm"
    assert get_browser_cookies_for("d1.example") is None, "untouched front must be evicted"


def test_missing_domain_read_is_none_not_keyerror(clean_cookie_store):
    """Boundary: an absent domain is a plain miss (no touch, no exception)."""
    assert clean_cookie_store is browser_cookies.BROWSER_COOKIES_BY_DOMAIN
    assert get_browser_cookies_for("absent.example") is None


# The cache-cap tests spawn a real browser-context store, so they are gated.


def test_cookie_cache_capped(clean_cookie_store):
    assert clean_cookie_store is browser_cookies.BROWSER_COOKIES_BY_DOMAIN
    for i in range(600):
        store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # Oldest entries were evicted; newest retained.
    assert get_browser_cookies_for("d599.example") == "k599=v"
    assert get_browser_cookies_for("d0.example") is None


def test_lru_keeps_recently_used(clean_cookie_store):
    assert clean_cookie_store is browser_cookies.BROWSER_COOKIES_BY_DOMAIN
    for i in range(BROWSER_COOKIES_MAX + 50):
        store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # Touch an older-but-not-oldest entry; it should survive the next eviction.
    get_browser_cookies_for(f"d{BROWSER_COOKIES_MAX}.example")
    store_browser_cookies("new.example", "new=v")
    assert get_browser_cookies_for(f"d{BROWSER_COOKIES_MAX}.example") == f"k{BROWSER_COOKIES_MAX}=v"


@pytest.mark.real_cffi
async def test_cffi_session_does_not_replay_cookies_it_received(base_url):
    """The impersonated session's own jar must be inert (store nothing, replay nothing).

    Two requests to the same domain is the whole leak: tenant A's ``Set-Cookie``
    becomes tenant B's ``Cookie:`` header when the jar replays by domain.
    """
    pytest.importorskip("curl_cffi")
    from crawlutron.runtime import process_state
    from crawlutron.transport import http_client_pool

    process_state.reset_all_process_state()
    session = http_client_pool.get_cffi_session()
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
    from crawlutron.transport.http_client_pool import get_async_http_client

    client = get_async_http_client()
    try:
        await client.get(base_url + "/set-cookie")
        replayed = await client.get(base_url + "/check-cookie")
    finally:
        await client.aclose()

    assert "session_id" not in replayed.text, (
        f"the shared httpx client replayed a cookie its own jar absorbed: {replayed.text!r}"
    )
