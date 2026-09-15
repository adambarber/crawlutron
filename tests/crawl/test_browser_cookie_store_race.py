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

import sys
import threading
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
    """The regression: eviction racing the read path's LRU touch.

    Writers keep the store pinned at its cap, so every store evicts the
    front-most key; readers keep re-reading the front-most keys, so the
    eviction lands inside the read window as often as the GIL allows. With no
    synchronization that raises ``KeyError`` out of ``move_to_end``; under a
    correct guard it cannot happen at all.
    """
    writers = 4
    readers = 4
    hot_keys = [f"hot{i}.example" for i in range(_BROWSER_COOKIES_MAX)]
    for key in hot_keys:
        _store_browser_cookies(key, f"v={key}")

    errors: list[BaseException] = []
    stop = threading.Event()
    ready = threading.Barrier(writers + readers)

    def writer(worker_id: int) -> None:
        try:
            ready.wait()
            for i in range(4000):
                _store_browser_cookies(f"w{worker_id}-{i}.example", f"v{i}")
        except Exception as exc:  # collected and re-raised in the main thread
            errors.append(exc)
        finally:
            stop.set()

    def reader(worker_id: int) -> None:
        try:
            ready.wait()
            while not stop.is_set():
                # Alternate the front-most keys: whichever one is about to be
                # evicted is exactly the one the touch can miss.
                for offset in range(8):
                    get_browser_cookies_for(hot_keys[worker_id * 8 + offset])
        except Exception as exc:  # collected and re-raised in the main thread
            errors.append(exc)

    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)  # widen the unsynchronized window on old code
    try:
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(writers)]
        threads += [threading.Thread(target=reader, args=(i,)) for i in range(readers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert not any(thread.is_alive() for thread in threads), "cookie store deadlock"
    finally:
        sys.setswitchinterval(previous_interval)
        stop.set()

    assert not errors, errors


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
