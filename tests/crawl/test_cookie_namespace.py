"""Per-request cookie namespace isolates concurrent crawls.

Without isolation, the shared ``_GLOBAL_COOKIE_JAR`` would leak cookies from
one tenant to another. The
namespace contextvar is set per ``crawl_request`` from
``request_profile.cookie_namespace`` so cookies are keyed by tenant.
"""

from app.crawl.strategies.cookie_jar import COOKIE_NAMESPACE, CookieJar
from app.crawl.strategies.request_profile import cookie_namespace_for
from tests._cookie_helpers import cookie_file, read_cookie_file

TENANT_A = "tenant_a"
TENANT_B = "tenant_b"


def two_tenant_jar() -> CookieJar:
    """A jar holding one cookie per tenant, both for the same host.

    Every operation under test here -- read back, clear one namespace, clear
    everything -- is a question about whether the two tenants stayed apart, and
    that question is only asked against a jar where each tenant has something to
    lose. Staging it once keeps a test from quietly proving isolation with a jar
    that only ever held one cookie.
    """
    jar = CookieJar()
    jar.add_cookie_header("http://x.example/", "a=1", namespace=TENANT_A)
    jar.add_cookie_header("http://x.example/", "b=2", namespace=TENANT_B)
    return jar


def test_namespace_isolation_prevents_cross_tenant_leak():
    jar = two_tenant_jar()
    # Neither tenant sees the other's cookie
    assert jar.cookie_header_for("http://x.example/", namespace=TENANT_A) == "a=1"
    assert jar.cookie_header_for("http://x.example/", namespace=TENANT_B) == "b=2"
    # Default namespace still empty
    assert jar.cookie_header_for("http://x.example/") == ""


def test_empty_namespace_uses_legacy_shared_slot():
    jar = CookieJar()
    jar.add_cookie_header("http://x.example/p", "k=v; Path=/", namespace="")
    # Same call without explicit namespace still sees it (legacy behavior)
    assert jar.cookie_header_for("http://x.example/p", namespace="") == "k=v"
    assert jar.cookie_header_for("http://x.example/p") == "k=v"


def test_clear_with_namespace_only_clears_that_namespace():
    jar = two_tenant_jar()
    jar.clear(namespace=TENANT_A)
    assert jar.cookie_header_for("http://x.example/", namespace=TENANT_A) == ""
    assert jar.cookie_header_for("http://x.example/", namespace=TENANT_B) == "b=2"


def test_clear_without_namespace_clears_everything():
    jar = two_tenant_jar()
    jar.clear()
    assert jar.cookie_header_for("http://x.example/", namespace=TENANT_A) == ""
    assert jar.cookie_header_for("http://x.example/", namespace=TENANT_B) == ""


def test_save_and_load_roundtrip_uses_legacy_namespace():
    """Persisted cookies live in the shared namespace; per-namespace cookies
    are intentionally not persisted so each caller can manage them itself."""
    jar = CookieJar()
    jar.add_cookie_header("http://shared.example/", "sess=abc", namespace="")
    jar.add_cookie_header("http://shared.example/", "sess=other", namespace="tenant_b")
    with cookie_file(jar, "ck_ns_roundtrip.json") as target:
        # Only the legacy (empty-namespace) entry is persisted.
        persisted = read_cookie_file(target)
        assert "shared.example" in persisted
        assert persisted["shared.example"] == {"sess": "abc"}
        # Roundtrip preserves the legacy namespace slot.
        loaded = CookieJar.import_from_file(target)
        assert loaded.cookie_header_for("http://shared.example/") == "sess=abc"
        assert loaded.cookie_header_for("http://shared.example/", namespace="tenant_b") == ""


def test_cookie_namespace_for_helper_reads_profile():
    assert cookie_namespace_for({"cookie_namespace": "tenant_a"}) == "tenant_a"
    assert cookie_namespace_for({"cookie_namespace": "  tenant_b  "}) == "tenant_b"
    assert cookie_namespace_for({}) == ""
    assert cookie_namespace_for({"cookie_namespace": 42}) == ""  # non-str ignored


def test_cookie_namespace_contextvar_default_is_empty():
    assert COOKIE_NAMESPACE.get() == ""
