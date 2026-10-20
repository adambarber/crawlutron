"""Per-request cookie namespace isolates concurrent crawls.

Without isolation the shared jar leaks cookies across tenants; the namespace
contextvar is set per ``crawl_request`` from ``cookie_namespace``, keying jars.
"""

from app.storage.coordination_cookies import DEFAULT_MAX_HOSTS_PER_NAMESPACE
from app.transport.cookie_jar import COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE, COOKIE_NAMESPACE, CookieJar
from app.transport.request_profile import cookie_namespace_for
from tests._cookie_helpers import cookie_file, read_cookie_file
from tests._policy_factories import canonical

TENANT_A = "tenant_a"
TENANT_B = "tenant_b"


def two_tenant_jar() -> CookieJar:
    """A jar holding one cookie per tenant, both for the same host.

    Every operation here is a question about whether the two tenants stayed
    apart, only asked against a jar where each tenant has something to lose.
    """
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://x.example/"), "a=1", namespace=TENANT_A)
    jar.add_cookie_header(canonical("http://x.example/"), "b=2", namespace=TENANT_B)
    return jar


def test_namespace_isolation_prevents_cross_tenant_leak():
    jar = two_tenant_jar()
    # Neither tenant sees the other's cookie
    assert jar.cookie_header_for(canonical("http://x.example/"), namespace=TENANT_A) == "a=1"
    assert jar.cookie_header_for(canonical("http://x.example/"), namespace=TENANT_B) == "b=2"
    # Default namespace still empty
    assert jar.cookie_header_for(canonical("http://x.example/")) == ""


def test_empty_namespace_uses_legacy_shared_slot():
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://x.example/p"), "k=v; Path=/", namespace="")
    # Same call without explicit namespace still sees it (legacy behavior)
    assert jar.cookie_header_for(canonical("http://x.example/p"), namespace="") == "k=v"
    assert jar.cookie_header_for(canonical("http://x.example/p")) == "k=v"


def test_clear_with_namespace_only_clears_that_namespace():
    jar = two_tenant_jar()
    jar.clear(namespace=TENANT_A)
    assert jar.cookie_header_for(canonical("http://x.example/"), namespace=TENANT_A) == ""
    assert jar.cookie_header_for(canonical("http://x.example/"), namespace=TENANT_B) == "b=2"


def test_clear_without_namespace_clears_everything():
    jar = two_tenant_jar()
    jar.clear()
    assert jar.cookie_header_for(canonical("http://x.example/"), namespace=TENANT_A) == ""
    assert jar.cookie_header_for(canonical("http://x.example/"), namespace=TENANT_B) == ""


def test_save_and_load_roundtrip_uses_legacy_namespace():
    """Persisted cookies live in the shared namespace; per-namespace cookies
    are intentionally not persisted so each caller can manage them itself."""
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://shared.example/"), "sess=abc", namespace="")
    jar.add_cookie_header(canonical("http://shared.example/"), "sess=other", namespace="tenant_b")
    with cookie_file(jar, "ck_ns_roundtrip.json") as target:
        # Only the legacy (empty-namespace) entry is persisted.
        persisted = read_cookie_file(target)
        assert "shared.example" in persisted
        assert persisted["shared.example"] == {"sess": "abc"}
        # Roundtrip preserves the legacy namespace slot.
        loaded = CookieJar.import_from_file(target)
        assert loaded.cookie_header_for(canonical("http://shared.example/")) == "sess=abc"
        assert loaded.cookie_header_for(canonical("http://shared.example/"), namespace="tenant_b") == ""


def test_cookie_namespace_for_helper_reads_profile():
    assert cookie_namespace_for({"cookie_namespace": "tenant_a"}) == "tenant_a"
    assert cookie_namespace_for({"cookie_namespace": "  tenant_b  "}) == "tenant_b"
    assert cookie_namespace_for({}) == ""
    assert cookie_namespace_for({"cookie_namespace": 42}) == ""  # non-str ignored


def test_cookie_namespace_contextvar_default_is_empty():
    assert COOKIE_NAMESPACE.get() == ""


def _cookie_jar_max_hosts() -> int:
    """The jar's per-namespace LRU cap, read from the module that owns it."""
    return COOKIE_JAR_MAX_HOSTS_PER_NAMESPACE


def test_one_tenant_crawl_does_not_evict_sibling_tenant_sessions():
    """The LRU cap is per-namespace: tenant A flooding past the cap must not
    evict tenant B's held session (the adjudicated #234 isolation shape)."""
    cap = _cookie_jar_max_hosts()
    jar = CookieJar()
    jar.add_cookie_header(canonical("http://held.example/"), "b=1; Path=/", namespace=TENANT_B)
    for i in range(cap + 5):
        jar.add_cookie_header(canonical(f"http://host{i}.example/"), "a=1; Path=/", namespace=TENANT_A)
    assert jar.cookie_header_for(canonical("http://held.example/"), namespace=TENANT_B) == "b=1"
    # A's flood is bounded by its own cap (its oldest is evicted, its newest survives)
    # while B's slot still lives alongside it -- per-namespace isolation, behaviorally.
    assert jar.cookie_header_for(canonical("http://host0.example/"), namespace=TENANT_A) == ""
    assert jar.cookie_header_for(canonical(f"http://host{cap + 4}.example/"), namespace=TENANT_A) == "a=1"


def test_store_backed_crawl_does_not_evict_sibling_tenant_past_cap(tmp_path):
    """The store-backed eviction is also per-namespace (#234). A floods first, then
    B writes, so the global budget is already spent; B's single slot must survive.
    """
    from app.storage.coordination_cookies import SharedCookieStore

    jar = CookieJar(
        SharedCookieStore(tmp_path / "coordination.db", max_hosts=DEFAULT_MAX_HOSTS_PER_NAMESPACE)
    )
    for i in range(DEFAULT_MAX_HOSTS_PER_NAMESPACE + 5):
        jar.add_cookie_header(canonical(f"http://host{i}.example/"), "a=1; Path=/", namespace=TENANT_A)
    jar.add_cookie_header(canonical("http://held.example/"), "b=1; Path=/", namespace=TENANT_B)
    assert jar.cookie_header_for(canonical("http://held.example/"), namespace=TENANT_B) == "b=1"
