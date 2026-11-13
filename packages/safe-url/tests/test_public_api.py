"""Public API contract for the safe_url package (issue #998 scope)."""

from __future__ import annotations

import logging

import pytest
from safe_url import (
    AddressClass,
    HostPolicy,
    NormalizedUrl,
    admit_or_none,
    admit_with_reason,
    classify,
    host_egress_reason,
    host_policy_reason,
    is_loopback_bind,
    is_public_suffix,
    is_secret_name,
    redact_url,
    redact_urls_in_text,
    registrable_domain,
    scope_allows,
)
from safe_url.host_policy import allow_private_targets, reset_private_target_memo
from safe_url.safe_regex import compile_untrusted

UNRESTRICTED = HostPolicy.unrestricted()
STRICT = HostPolicy.from_strings("example.com", "")


@pytest.fixture(autouse=True)
def _reset_private_target_state(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    reset_private_target_memo()


def test_normalized_url_canonicalizes_case_and_tracking_params():
    url = NormalizedUrl("HTTPS://Example.COM:443/a/b/?utm_source=x#frag", policy=UNRESTRICTED)
    assert url.raw == "https://example.com/a/b/"
    assert url.host == "example.com"
    assert url.site == "example.com"


def test_normalized_url_refuses_private_targets():
    with pytest.raises(ValueError, match="private"):
        NormalizedUrl("http://127.0.0.1/x", policy=UNRESTRICTED)


def test_admit_or_none_refuses_private_targets():
    assert admit_or_none("http://127.0.0.1/x", policy=UNRESTRICTED) is None
    assert admit_or_none(None, policy=UNRESTRICTED) is None
    assert admit_or_none("", policy=UNRESTRICTED) is None


def test_admit_with_reason_surfaces_refusal():
    url, reason = admit_with_reason("http://127.0.0.1/x", policy=UNRESTRICTED)
    assert url is None
    assert reason == "private_network_target"


def test_host_policy_from_strings_parses():
    policy = HostPolicy.from_strings("example.com,*.trusted.example", "*.ads.example")
    assert "example.com" in policy.allowed_hosts
    assert "trusted.example" in policy.allowed_hosts
    assert "ads.example" in policy.denied_hosts


def test_host_policy_reason_denies_blocked_host():
    policy = HostPolicy.from_strings("", "evil.test")
    assert host_policy_reason("evil.test", policy=policy) == "host_denied"


def test_host_policy_reason_allows_listed_host():
    policy = HostPolicy.from_strings("example.com", "")
    assert host_policy_reason("example.com", policy=policy) is None


def test_host_egress_reason_blocks_private():
    assert host_egress_reason("127.0.0.1") == "private_network_target"
    assert host_egress_reason("8.8.8.8") is None


def test_garbage_private_target_env_warns_and_falls_back(monkeypatch, caplog):
    """CRAWLUTRON_ALLOW_PRIVATE_TARGETS=2 warns and falls back to the secure default."""
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "2")
    reset_private_target_memo()
    caplog.set_level(logging.WARNING, logger="safe_url.host_policy")
    assert allow_private_targets() is False
    assert any(
        "invalid_bool_env for CRAWLUTRON_ALLOW_PRIVATE_TARGETS" in r.getMessage() for r in caplog.records
    )


def test_address_classification_precedence():
    assert classify("127.0.0.1") is AddressClass.LOOPBACK
    assert classify("10.0.0.1") is AddressClass.PRIVATE
    assert classify("8.8.8.8") is AddressClass.PUBLIC
    assert classify("example.com") is AddressClass.NOT_AN_ADDRESS


def test_is_loopback_bind():
    assert is_loopback_bind("127.0.0.1") is True
    assert is_loopback_bind("localhost") is True
    assert is_loopback_bind("10.0.0.1") is False


def test_registrable_domain_and_public_suffix():
    assert registrable_domain("a.b.c.example.com") == "example.com"
    assert registrable_domain("1.2.3.4") == "1.2.3.4"
    assert registrable_domain("a.b.example.co.uk") == "example.co.uk"
    assert registrable_domain("a.b.c.example.com", psl=None) == "example.com"
    assert is_public_suffix("com") is True
    assert is_public_suffix("example.com") is False


def test_idna_host_punycode():
    from safe_url.host_canonical import idna_host

    assert idna_host("Example.COM") == "example.com"
    assert idna_host("bücher.example") == "xn--bcher-kva.example"


def test_redact_url_masks_credentials():
    assert "pass" not in redact_url("https://user:pass@example.com/hook")
    assert "secret" not in redact_url("https://example.com/hook?key=secret")
    assert "example.com" in redact_url("https://example.com/hook")


def test_redact_urls_in_text_masks_urls():
    text = "failed https://example.com/hook?token=abc now"
    assert "abc" not in redact_urls_in_text(text)
    assert "failed" in redact_urls_in_text(text)


def test_is_secret_name_vocabulary():
    assert is_secret_name("api_key") is True
    assert is_secret_name("token") is True
    assert is_secret_name("design") is False


def test_scope_allows_matches_against_url_without_query():
    compiled = compile_untrusted(r"example\.com/keep/")
    assert scope_allows(compiled, "https://example.com/keep/page?x=1") is True
    assert scope_allows(compiled, "https://example.com/drop/page") is False
