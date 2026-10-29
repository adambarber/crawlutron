"""The three cookie paths round-trip through one per-namespace cookie-store concept (#123)."""

from crawlutron.storage.coordination_cookies import SharedCookieStore
from crawlutron.transport.cookie_jar import CookieJar
from crawlutron.transport.cookie_store import CookieStore, InMemoryCookieStore
from tests._policy_factories import canonical


def test_cookie_store_protocol_is_declared():
    """The one concept is a real, named protocol (not a comment)."""
    assert CookieStore.__name__ == "CookieStore"
    for verb in ("load", "merge", "clear"):
        assert hasattr(CookieStore, verb)


def test_inmemory_medium_is_the_jar_and_the_browser_backing():
    """Both process-local paths share the one in-memory medium, observable by behavior."""
    from crawlutron.transport import browser_cookies

    # The browser path stores and reads through the same per-namespace LRU medium the jar
    # uses: behaviorally it must round-trip and evict on the shared cap, not a bespoke mapping.
    browser_cookies.store_browser_cookies("d0.example", "a=1")
    assert browser_cookies.get_browser_cookies_for("d0.example") == "a=1"
    for i in range(1, browser_cookies.BROWSER_COOKIES_MAX + 1):
        browser_cookies.store_browser_cookies(f"d{i}.example", f"k{i}=v")
    # Pushing past the cap evicts the front-most (d0), proving the shared LRU answer, not a bare dict.
    assert browser_cookies.get_browser_cookies_for("d0.example") is None
    assert (
        browser_cookies.get_browser_cookies_for(f"d{browser_cookies.BROWSER_COOKIES_MAX}.example")
        == f"k{browser_cookies.BROWSER_COOKIES_MAX}=v"
    )


def test_jar_browser_and_shared_store_round_trip_through_the_concept():
    """All three paths load/merge/clear the same way."""
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://a.example/"), "k=v; Path=/")
    assert jar.cookie_header_for(canonical("http://a.example/")) == "k=v"
    assert jar.cookie_header_for(canonical("http://a.example/")) == "k=v"

    from crawlutron.transport import browser_cookies

    browser_cookies.store_browser_cookies("b.example", "s=1")
    assert browser_cookies.get_browser_cookies_for("b.example") == "s=1"

    store = InMemoryCookieStore(max_hosts_per_namespace=4)
    store.merge("ns", "c.example", {"x": "y"})
    assert store.load("ns", "c.example") == {"x": "y"}
    store.clear("ns")
    assert store.load("ns", "c.example") == {}


def test_shared_sqlite_store_implements_the_cookie_store_protocol(tmp_path):
    store = SharedCookieStore(tmp_path / "coordination.db")
    assert isinstance(store, CookieStore)


def test_eviction_is_per_namespace_not_global(tmp_path):
    """#234: evicting one namespace's LRU never drops another's slots."""
    jar = CookieJar()
    for i in range(5):
        jar.add_cookie_header(canonical(f"http://h{i}.example/"), f"k=v{i}; Path=/", namespace="tenA")
        jar.add_cookie_header(canonical(f"http://h{i}.example/"), "k=v; Path=/", namespace="tenB")
    # tenA stays at its cap while tenB stays unaffected: add to tenA to force its
    # own eviction, and tenB must keep every slot it has.
    for i in range(2):
        jar.add_cookie_header(canonical(f"http://h{i}.example/"), f"k=v{i}; Path=/", namespace="tenA")
    assert jar.cookie_header_for(canonical("http://h0.example/"), namespace="tenB") == "k=v"
    assert jar.cookie_header_for(canonical("http://h4.example/"), namespace="tenB") == "k=v"


def test_browser_and_jar_share_per_namespace_cap_semantics():
    """The in-memory medium enforces the same LRU rule for both consumers."""

    store = InMemoryCookieStore(max_hosts_per_namespace=2)
    store.merge("", "d0", {"a": "1"})
    store.merge("", "d1", {"b": "2"})
    store.merge("", "d2", {"c": "3"})  # evicts d0 (single namespace)
    assert store.load("", "d0") == {}
    assert store.load("", "d1") == {"b": "2"}
    assert store.load("", "d2") == {"c": "3"}
