"""Browser cookie store: cap, LRU eviction, isolation and concurrency."""

import threading
from collections import OrderedDict

import pytest

import app.transport.browser_cookies as browser_cookies
from app.transport import browser as browser_mod
from app.transport.browser import get_browser_cookies_for
from app.transport.browser_cookies import BROWSER_COOKIES_MAX, store_browser_cookies
from tests._lock_doubles import DelegatingLock


@pytest.fixture
def clean_cookie_store():
    """The process-global cookie store, emptied for this test and left empty.

    Resetting handily needs no surface the production path lacks, which was
    the reason save-and-restore only ever worked by ``MutableMapping``.
    """
    store = browser_cookies.BROWSER_COOKIES_BY_DOMAIN
    browser_mod.reset_process_state()
    try:
        yield store
    finally:
        browser_mod.reset_process_state()


def test_public_helpers_reach_the_guarded_store(clean_cookie_store):
    """The guarded object is the one the public helpers actually use.

    A bare ``OrderedDict`` at every site made the race possible; the claim is
    the helpers cannot be holding an unguarded mapping.
    """
    store_browser_cookies("guarded.example", "sess=1")
    assert get_browser_cookies_for("guarded.example") == "sess=1"


def test_concurrent_read_and_eviction_does_not_raise(clean_cookie_store):
    """The regression, scheduled rather than left to GIL luck.

    The pre-fix ``get`` then ``move_to_end`` had no sync, so an eviction in
    that window raised ``KeyError``; the interleaving is deterministic here.
    """
    for i in range(BROWSER_COOKIES_MAX):
        store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # The front-most key: the one popitem(last=False) drops first.
    victim = "d0.example"

    # --- 1: the unsynchronized shape, with the read window held open --------
    old_shape = OrderedDict((f"d{i}.example", f"k{i}=v") for i in range(BROWSER_COOKIES_MAX))
    old_error: list[BaseException] = []
    reader_in_window = threading.Event()
    eviction_landed = threading.Event()

    def _racy_read() -> None:
        try:
            old_shape.get(victim)
            # The seam: pre-fix, this is the gap the GIL may or may not fill. Scheduling the eviction here
            # makes the race certain.
            reader_in_window.set()
            eviction_landed.wait(timeout=5)
            old_shape.move_to_end(victim)
        except BaseException as exc:  # collected and asserted in the main thread
            old_error.append(exc)

    def _evict() -> None:
        reader_in_window.wait(timeout=5)
        old_shape["brand.new.example"] = "v"
        while len(old_shape) > BROWSER_COOKIES_MAX:
            old_shape.popitem(last=False)  # drops the victim the reader is about to touch
        eviction_landed.set()

    race_threads = [threading.Thread(target=target) for target in (_racy_read, _evict)]
    for thread in race_threads:
        thread.start()
    for thread in race_threads:
        # The liveness check below is the real one.
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in race_threads), "the scheduled race wedged"

    assert len(old_error) == 1, f"the unsynchronized read was expected to raise exactly once, got {old_error}"
    assert isinstance(old_error[0], KeyError), (
        f"the unsynchronized read raised {old_error[0]!r}, not the expected KeyError"
    )

    # --- 2: the guarded store serializes the read, deterministically -------- The discriminator is the
    # store's own lock, not GIL timing: the read path must take that lock, and it must be able to get it once
    store = clean_cookie_store

    class _RecordingLock(DelegatingLock):
        """Delegating lock that records which thread acquired it, and how often.

        Per-thread, not just a total: the probe holds the lock, so a plain count
        would pass against the lock-free read this test exists to reject.
        """

        def __init__(self, inner):
            super().__init__(inner)
            self.acquisitions: dict[int, int] = {}

        def acquire(self, *args, **kwargs):
            me = threading.get_ident()
            self.acquisitions[me] = self.acquisitions.get(me, 0) + 1
            return super().acquire(*args, **kwargs)

        def locked(self):
            return self._inner.locked()

    real_lock = store._store._lock
    recording = _RecordingLock(real_lock)
    patch = pytest.MonkeyPatch()
    patch.setattr(store._store, "_lock", recording)
    box: dict[str, str | None] = {}
    lock_taken = threading.Event()
    probe_release = threading.Event()
    read_started = threading.Event()

    def _hold_the_lock() -> None:
        with recording:
            lock_taken.set()
            # Held until the main thread has seen the reader start, so the read is genuinely concurrent with a
            # lock holder.
            probe_release.wait(timeout=5)

    def _guarded_read() -> None:
        lock_taken.wait(timeout=5)
        read_started.set()
        box["value"] = get_browser_cookies_for(victim)

    probe = threading.Thread(target=_hold_the_lock)
    reader = threading.Thread(target=_guarded_read)
    probe.start()
    assert lock_taken.wait(timeout=5), "the lock holder never took the store lock"
    reader.start()
    assert read_started.wait(timeout=5) is True, "the reader never reached the read"
    probe_release.set()
    probe.join(timeout=5)
    reader.join(timeout=5)
    patch.undo()

    assert not probe.is_alive() and not reader.is_alive(), "the guarded read wedged"
    assert reader.ident is not None
    assert recording.acquisitions.get(reader.ident, 0) > 0, (
        "the read path never took the store lock: reads are unsynchronized and the eviction race is wide open"
    )
    assert box.get("value") is not None, "the read did not return the value once the lock was released"


def test_read_touches_lru_so_a_read_key_survives_eviction(clean_cookie_store):
    """Inverse case: the guard must not have changed LRU semantics.

    A read refreshes recency, so the key survives the next eviction while the
    front-most untouched key is the one dropped.
    """
    for i in range(BROWSER_COOKIES_MAX):
        store_browser_cookies(f"d{i}.example", f"k{i}=v")
    oldest = "d0.example"
    assert get_browser_cookies_for(oldest) == "k0=v"
    store_browser_cookies("new.example", "new=v")

    assert get_browser_cookies_for(oldest) == "k0=v", "read must keep the key warm"
    assert get_browser_cookies_for("d1.example") is None, "untouched front must be evicted"


def test_missing_domain_read_is_none_not_keyerror(clean_cookie_store):
    """Boundary: an absent domain is a plain miss (no touch, no exception)."""
    assert get_browser_cookies_for("absent.example") is None


# The cache-cap tests spawn a real browser-context store, so they are gated.


def test_cookie_cache_capped(clean_cookie_store):
    for i in range(600):
        store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # Oldest entries were evicted; newest retained.
    assert get_browser_cookies_for("d599.example") == "k599=v"
    assert get_browser_cookies_for("d0.example") is None


def test_lru_keeps_recently_used(clean_cookie_store):
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
    from app.transport import http_client_pool

    http_client_pool.reset_process_state()
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
    from app.transport.http_client_pool import get_async_http_client

    client = get_async_http_client()
    try:
        await client.get(base_url + "/set-cookie")
        replayed = await client.get(base_url + "/check-cookie")
    finally:
        await client.aclose()

    assert "session_id" not in replayed.text, (
        f"the shared httpx client replayed a cookie its own jar absorbed: {replayed.text!r}"
    )
