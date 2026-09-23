"""Adversarial fix: per-request cookie namespace isolates concurrent crawls.

Without isolation, the shared ``_GLOBAL_COOKIE_JAR`` would leak cookies from
one tenant to another (adversarial review F-cookie-cross-contamination). The
namespace contextvar is set per ``crawl_request`` from
``request_profile.cookie_namespace`` so cookies are keyed by tenant.
"""

from app.crawl.strategies.cookie_jar import _COOKIE_NAMESPACE, CookieJar
from app.crawl.strategies.request_profile import cookie_namespace_for


def test_namespace_isolation_prevents_cross_tenant_leak():
    jar = CookieJar()
    # Tenant A sets a session cookie
    jar.add_cookie_header("http://shared.example/", "sess=A; Path=/", namespace="tenant_a")
    # Tenant B sets a different session cookie
    jar.add_cookie_header("http://shared.example/", "sess=B; Path=/", namespace="tenant_b")
    # Neither tenant sees the other's cookie
    assert jar.cookie_header_for("http://shared.example/", namespace="tenant_a") == "sess=A"
    assert jar.cookie_header_for("http://shared.example/", namespace="tenant_b") == "sess=B"
    # Default namespace still empty
    assert jar.cookie_header_for("http://shared.example/") == ""


def test_empty_namespace_uses_legacy_shared_slot():
    jar = CookieJar()
    jar.add_cookie_header("http://x.example/p", "k=v; Path=/", namespace="")
    # Same call without explicit namespace still sees it (legacy behavior)
    assert jar.cookie_header_for("http://x.example/p", namespace="") == "k=v"
    assert jar.cookie_header_for("http://x.example/p") == "k=v"


def test_clear_with_namespace_only_clears_that_namespace():
    jar = CookieJar()
    jar.add_cookie_header("http://x.example/", "a=1", namespace="tenant_a")
    jar.add_cookie_header("http://x.example/", "b=2", namespace="tenant_b")
    jar.clear(namespace="tenant_a")
    assert jar.cookie_header_for("http://x.example/", namespace="tenant_a") == ""
    assert jar.cookie_header_for("http://x.example/", namespace="tenant_b") == "b=2"


def test_clear_without_namespace_clears_everything():
    jar = CookieJar()
    jar.add_cookie_header("http://x.example/", "a=1", namespace="tenant_a")
    jar.add_cookie_header("http://x.example/", "b=2", namespace="tenant_b")
    jar.clear()
    assert jar.cookie_header_for("http://x.example/", namespace="tenant_a") == ""
    assert jar.cookie_header_for("http://x.example/", namespace="tenant_b") == ""


def test_save_and_load_roundtrip_uses_legacy_namespace():
    """Persisted cookies live in the shared namespace; per-namespace cookies
    are intentionally not persisted so each caller can manage them itself."""
    import json
    import os
    import tempfile

    jar = CookieJar()
    jar.add_cookie_header("http://shared.example/", "sess=abc", namespace="")
    jar.add_cookie_header("http://shared.example/", "sess=other", namespace="tenant_b")
    target = os.path.join(tempfile.gettempdir(), "ck_ns_roundtrip.json")
    try:
        jar.save_to_file(target)
        with open(target, encoding="utf-8") as f:
            persisted = json.load(f)
        # Only the legacy (empty-namespace) entry is persisted.
        assert "shared.example" in persisted
        assert persisted["shared.example"] == {"sess": "abc"}
        # Roundtrip preserves the legacy namespace slot.
        loaded = CookieJar.load_from_file(target)
        assert loaded.cookie_header_for("http://shared.example/") == "sess=abc"
        assert loaded.cookie_header_for("http://shared.example/", namespace="tenant_b") == ""
    finally:
        if os.path.exists(target):
            os.unlink(target)


def test_cookie_namespace_for_helper_reads_profile():
    assert cookie_namespace_for({"cookie_namespace": "tenant_a"}) == "tenant_a"
    assert cookie_namespace_for({"cookie_namespace": "  tenant_b  "}) == "tenant_b"
    assert cookie_namespace_for({}) == ""
    assert cookie_namespace_for({"cookie_namespace": 42}) == ""  # non-str ignored


def test_cookie_namespace_contextvar_default_is_empty():
    assert _COOKIE_NAMESPACE.get() == ""
