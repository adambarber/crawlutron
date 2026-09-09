"""Verify the ``ssrf_guard_required`` fixture enforces opt-out.

These tests are not testing SSRF behavior itself (covered by ``test_ssrf_*``);
they confirm the conftest fixture refuses to let a test exercise the SSRF
helpers without first calling ``block_private_targets``.
"""

import pytest

from app.crawl.strategies import http as http_mod


def test_ssrf_fixture_passes_when_opted_out(monkeypatch, ssrf_guard_required):
    """The happy path: opt out, then exercise the helper. Should pass."""
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")
    # Call the real helper; it will fail to resolve but the call itself
    # counts as "exercised", which is what the fixture checks.
    try:
        http_mod._check_ssrf_protection("http://example.invalid/")
    except Exception:
        pass


def test_ssrf_fixture_fails_when_opt_out_missing(monkeypatch, ssrf_guard_required):
    """If the test forgets to opt out, the fixture must fail with a clear
    message naming the missing opt-out.
    """
    # Deliberately do NOT set BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=0.
    # The fixture must catch this.
    import os

    monkeypatch.delenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", raising=False)
    os.environ["BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS"] = "1"
    with pytest.raises(pytest.fail.Exception, match="SSRF guard"):
        http_mod._check_ssrf_protection("http://example.invalid/")


# Note: a test that requests ``ssrf_guard_required`` but never touches
# ``_check_ssrf_protection`` or ``_resolve_and_pin_host`` simply passes
# without exercising the SSRF path. The fixture catches opt-out misses
# in the per-call wrapper, which fires during the test body and is
# catchable via ``pytest.raises``.
