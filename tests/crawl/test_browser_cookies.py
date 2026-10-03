"""Browser cookie store: cap, LRU eviction, isolation and concurrency."""

import threading
from collections import OrderedDict

import pytest

from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies.browser import _BROWSER_COOKIES_MAX, _store_browser_cookies, get_browser_cookies_for
from tests._lock_doubles import DelegatingLock


@pytest.fixture
def clean_cookie_store():
    """The process-global cookie store, emptied for this test and left empty.

    Save-and-restore was ``dict(store)`` / ``store.update(saved)``, which only
    worked because the store implemented ``MutableMapping``. For a process global
    whose whole purpose is to be reset, leaving it empty is the cleaner end state
    and needs no surface the production path does not already have.
    """
    store = browser_mod._BROWSER_COOKIES_BY_DOMAIN
    browser_mod.reset_process_state()
    try:
        yield store
    finally:
        browser_mod.reset_process_state()


def test_public_helpers_reach_the_guarded_store(clean_cookie_store):
    """The guarded object is the one the public helpers actually use.

    A bare ``OrderedDict`` handed to every call site is what made the race
    possible: callers had to remember to synchronize, and none did. So the claim
    is not that the store *has* a lock -- it is that the helpers it is reached
    through cannot be holding a different, unguarded mapping.
    """
    _store_browser_cookies("guarded.example", "sess=1")
    assert get_browser_cookies_for("guarded.example") == "sess=1"


def test_concurrent_read_and_eviction_does_not_raise(clean_cookie_store):
    """The regression, scheduled rather than left to GIL luck.

    The pre-fix store was a bare ``OrderedDict`` and its read path was
    ``get(domain)`` followed by ``move_to_end(domain)`` with no
    synchronization between them. A concurrent write past the cap evicts the
    front-most key with ``popitem(last=False)``, so an eviction landing in that
    read window makes the touch raise ``KeyError`` out of a live crawl. Left
    to the GIL, landing there is timing luck -- this test observed passing
    ~2/3 of the time against the pre-fix code -- so the interleaving is
    scheduled explicitly instead.

    Two coordinated halves, each deterministic:

    1. The old-code shape reproduced with a seam: the read yields between
       ``get`` and ``move_to_end`` and the evictor runs in that exact window,
       so the ``KeyError`` is *guaranteed*. This is the defect the guard must
       close, no longer dependent on the GIL choosing to land the eviction in
       the window.
    2. The guarded store's own lock, held by a probe: a concurrent read must
       wait for it rather than run alongside -- the read path is serialized,
       which is what makes the window in (1) unreachable. An unguarded store
       has no lock to hold, so this half is red against the pre-fix store on
       that fact alone.
    """
    for i in range(_BROWSER_COOKIES_MAX):
        _store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # The front-most key: the one popitem(last=False) drops first.
    victim = "d0.example"

    # --- 1: the unsynchronized shape, with the read window held open --------
    old_shape = OrderedDict((f"d{i}.example", f"k{i}=v") for i in range(_BROWSER_COOKIES_MAX))
    old_error: list[BaseException] = []
    reader_in_window = threading.Event()
    eviction_landed = threading.Event()

    def _racy_read() -> None:
        try:
            old_shape.get(victim)
            # The seam: pre-fix, this is the gap the GIL may or may not fill.
            # Scheduling the eviction here makes the race certain.
            reader_in_window.set()
            eviction_landed.wait(timeout=5)
            old_shape.move_to_end(victim)
        except BaseException as exc:  # collected and asserted in the main thread
            old_error.append(exc)

    def _evict() -> None:
        reader_in_window.wait(timeout=5)
        old_shape["brand.new.example"] = "v"
        while len(old_shape) > _BROWSER_COOKIES_MAX:
            old_shape.popitem(last=False)  # drops the victim the reader is about to touch
        eviction_landed.set()

    race_threads = [threading.Thread(target=target) for target in (_racy_read, _evict)]
    for thread in race_threads:
        thread.start()
    for thread in race_threads:
        # ``join`` is called for its effect and returns nothing; asserting on
        # its return was asserting on ``None`` and said nothing about whether
        # the thread finished. The liveness check below is the real one.
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in race_threads), "the scheduled race wedged"

    assert len(old_error) == 1, f"the unsynchronized read was expected to raise exactly once, got {old_error}"
    assert isinstance(old_error[0], KeyError), (
        f"the unsynchronized read raised {old_error[0]!r}, not the expected KeyError"
    )

    # --- 2: the guarded store serializes the read, deterministically --------
    # The discriminator is the store's own lock, not GIL timing: the read path
    # must take that lock, and it must be able to get it once the holder
    # releases. Both facts are observable without a stopwatch -- one from a
    # recording lock that counts acquisitions, one from an event handshake
    # between the probe and the reader. The previous version asserted "the
    # reader had not finished after 200ms", which is a bet about thread
    # scheduling that a store with NO lock would often satisfy by accident, and
    # that a slow box could break for no reason at all.
    store = clean_cookie_store
    assert store._lock is not None, (
        "the cookie store has no lock; reads are unsynchronized and the eviction race is wide open"
    )

    class _RecordingLock(DelegatingLock):
        """Delegating lock that records which thread acquired it, and how often.

        Per-thread, not just a total: the probe below deliberately holds the
        lock, so a plain acquisition count would be satisfied by the probe
        alone and would pass against exactly the lock-free read this test
        exists to reject.
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

    real_lock = store._lock
    recording = _RecordingLock(real_lock)
    patch = pytest.MonkeyPatch()
    patch.setattr(store, "_lock", recording)
    box: dict[str, str | None] = {}
    lock_taken = threading.Event()
    probe_release = threading.Event()
    read_started = threading.Event()

    def _hold_the_lock() -> None:
        with recording:
            lock_taken.set()
            # Held until the main thread has seen the reader start, so the read
            # is genuinely concurrent with a lock holder. This used to be
            # `read_done.wait(timeout=5)`, which is a deadlock the test then
            # waited out: the reader cannot finish until the probe lets go, and
            # the probe was waiting for the reader. Five seconds per run, and
            # nothing was being asserted for any of it.
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
    # ``Thread.ident`` is None only before ``start``, and this reader was
    # started above; a real join of the started thread is what makes that a
    # checked fact rather than an assumption.
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
    for i in range(_BROWSER_COOKIES_MAX):
        _store_browser_cookies(f"d{i}.example", f"k{i}=v")
    oldest = "d0.example"
    assert get_browser_cookies_for(oldest) == "k0=v"
    _store_browser_cookies("new.example", "new=v")

    assert get_browser_cookies_for(oldest) == "k0=v", "read must keep the key warm"
    assert get_browser_cookies_for("d1.example") is None, "untouched front must be evicted"


def test_missing_domain_read_is_none_not_keyerror(clean_cookie_store):
    """Boundary: an absent domain is a plain miss (no touch, no exception)."""
    assert get_browser_cookies_for("absent.example") is None


# The cache-cap tests spawn a real browser-context store, so they are gated.


def test_cookie_cache_capped(clean_cookie_store):
    for i in range(600):
        _store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # Oldest entries were evicted; newest retained.
    assert get_browser_cookies_for("d599.example") == "k599=v"
    assert get_browser_cookies_for("d0.example") is None


def test_lru_keeps_recently_used(clean_cookie_store):
    for i in range(_BROWSER_COOKIES_MAX + 50):
        _store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # Touch an older-but-not-oldest entry; it should survive the next eviction.
    get_browser_cookies_for(f"d{_BROWSER_COOKIES_MAX}.example")
    _store_browser_cookies("new.example", "new=v")
    assert get_browser_cookies_for(f"d{_BROWSER_COOKIES_MAX}.example") == f"k{_BROWSER_COOKIES_MAX}=v"


@pytest.mark.real_cffi
async def test_cffi_session_does_not_replay_cookies_it_received(base_url):
    """The impersonated session's own jar must be inert (store nothing, replay
    nothing); a cookie it saw on one request must never come back on the next.

    Two requests to the same domain is the whole leak: tenant A's Set-Cookie
    becomes tenant B's ``Cookie:`` header when the jar replays by domain.
    """
    pytest.importorskip("curl_cffi")
    from app.crawl.strategies.http_client_pool import _build_cffi_session

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
    from app.crawl.strategies.http_client_pool import _build_async_client

    client = _build_async_client(None)
    try:
        await client.get(base_url + "/set-cookie")
        replayed = await client.get(base_url + "/check-cookie")
    finally:
        await client.aclose()

    assert "session_id" not in replayed.text, (
        f"the shared httpx client replayed a cookie its own jar absorbed: {replayed.text!r}"
    )
