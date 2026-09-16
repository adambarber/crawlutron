"""P0-1: the browser cookie store must be internally synchronized.

``_BROWSER_COOKIES_BY_DOMAIN`` is a module-global LRU shared between the render
path (which writes harvested cookies) and the crawl engine (which reads them
for the cffi cookie-bootstrap retry). Historically it was a bare
``OrderedDict`` with no lock anywhere in the module, and the *read* path
mutated it: ``get(domain)`` followed by ``move_to_end(domain)``. A concurrent
write past the cap evicts with ``popitem(last=False)``, so the eviction could
land between the read and the touch and make ``move_to_end`` raise
``KeyError`` -- inside a live crawl, on the request path.

These tests are not marked ``browser``: the store is plain in-process state and
needs no Camoufox/Playwright, so they run in the default fast suite.
"""

import threading
import time
from collections import OrderedDict

import pytest

from app.crawl.strategies import browser as browser_mod
from app.crawl.strategies.browser import _BROWSER_COOKIES_MAX, _store_browser_cookies, get_browser_cookies_for


@pytest.fixture
def clean_cookie_store():
    """Snapshot/restore the module-global store so the tests stay isolated."""
    store = browser_mod._BROWSER_COOKIES_BY_DOMAIN
    saved = dict(store)
    store.clear()
    try:
        yield store
    finally:
        store.clear()
        store.update(saved)


def test_store_is_a_guarded_container_not_a_bare_ordered_dict(clean_cookie_store):
    """The invalid state must be unrepresentable at the container itself.

    A bare ``OrderedDict`` handed to every call site is what made the race
    possible: callers had to remember to synchronize, and none did. The store
    now owns its lock, so a lock-free mapping cannot be reached from the module.
    """
    assert not isinstance(clean_cookie_store, OrderedDict)
    assert hasattr(clean_cookie_store, "_lock")
    # The guarded object must be the one the public helpers actually use.
    _store_browser_cookies("guarded.example", "sess=1")
    assert get_browser_cookies_for("guarded.example") == "sess=1"
    assert len(clean_cookie_store) == 1


def test_concurrent_read_and_eviction_does_not_raise(clean_cookie_store, real_sleeps):
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
        time.sleep(0.02)  # let the reader settle inside the open window
        old_shape["brand.new.example"] = "v"
        while len(old_shape) > _BROWSER_COOKIES_MAX:
            old_shape.popitem(last=False)  # drops the victim the reader is about to touch
        eviction_landed.set()

    race_threads = [threading.Thread(target=target) for target in (_racy_read, _evict)]
    for thread in race_threads:
        thread.start()
    for thread in race_threads:
        assert thread.join(timeout=10) is None, "the scheduled race wedged"

    assert len(old_error) == 1, (
        f"the unsynchronized read was expected to raise exactly once, got {old_error}"
    )
    assert isinstance(old_error[0], KeyError), (
        f"the unsynchronized read raised {old_error[0]!r}, not the expected KeyError"
    )

    # --- 2: the guarded store serializes the read, deterministically --------
    # The discriminator is the store's own lock, not GIL timing: with it held
    # by a probe, a concurrent read must WAIT rather than observe a
    # half-evicted map. That is only true when the read path takes the lock,
    # so it is exactly what an unguarded store cannot fake.
    store = clean_cookie_store
    assert hasattr(store, "_lock"), (
        "the cookie store has no lock; reads are unsynchronized and the "
        "eviction race is wide open"
    )
    box: dict[str, str | None] = {}
    release_lock = threading.Event()

    def _hold_the_lock() -> None:
        with store._lock:
            release_lock.wait(timeout=5)

    def _guarded_read() -> None:
        box["value"] = get_browser_cookies_for(victim)

    probe = threading.Thread(target=_hold_the_lock)
    reader = threading.Thread(target=_guarded_read)
    probe.start()
    time.sleep(0.1)  # let the probe take the lock
    reader.start()
    time.sleep(0.2)
    assert "value" not in box, (
        "the read finished while another thread held the store lock; the read "
        "path is not serialized by the guard"
    )
    release_lock.set()
    reader.join(timeout=5)
    probe.join(timeout=5)
    assert box.get("value") is not None, (
        "the read did not return the value once the lock was released"
    )


def test_read_touches_lru_so_a_read_key_survives_eviction(clean_cookie_store):
    """Inverse case: the guard must not have changed LRU semantics.

    A read refreshes recency, so the key survives the next eviction while the
    front-most untouched key is the one dropped.
    """
    store = clean_cookie_store
    for i in range(_BROWSER_COOKIES_MAX):
        _store_browser_cookies(f"d{i}.example", f"k{i}=v")
    oldest = "d0.example"
    assert get_browser_cookies_for(oldest) == "k0=v"
    _store_browser_cookies("new.example", "new=v")

    assert get_browser_cookies_for(oldest) == "k0=v", "read must keep the key warm"
    assert get_browser_cookies_for("d1.example") is None, "untouched front must be evicted"
    assert len(store) == _BROWSER_COOKIES_MAX


def test_missing_domain_read_is_none_not_keyerror(clean_cookie_store):
    """Boundary: an absent domain is a plain miss (no touch, no exception)."""
    assert get_browser_cookies_for("absent.example") is None
    assert len(clean_cookie_store) == 0
