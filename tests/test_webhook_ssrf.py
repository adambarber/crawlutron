"""TDD tests for webhook SSRF (hostname not resolved): the validator must resolve
DNS and reject private/loopback targets, and the delivery POST must connect to a
pinned IP (DNS-rebinding safe) while preserving the original Host header.
"""

import pytest

from app.api.webhooks import (
    ensure_https_webhook_url,
)
from app.api.webhooks import (
    post_webhook as _post_webhook,
)
from app.api.webhooks import (
    validate_webhook_url as _validate_webhook_url,
)
from app.safety.validation import HostPolicy
from tests._assertions import (
    block_private_targets,
    host_resolves_to,
    plaintext_webhook_refusal,
)
from tests._webhook_doubles import assert_delivered_once, record_webhook_delivery


def _assert_pinned_delivery(delivered) -> None:
    """A delivery must connect to the pinned validated IP (never a re-resolved
    hostname) with the original Host header preserved -- DNS-rebinding safe."""
    assert_delivered_once(delivered)
    assert "8.8.8.8" in str(delivered[0].url), delivered
    assert delivered[0].headers.get("Host") == "webhook.example.com", delivered


class TestWebhookUrlValidation:
    def test_blocks_private_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a private IP must be rejected."""
        with host_resolves_to(monkeypatch, "127.0.0.1"), pytest.raises(ValueError):
            _validate_webhook_url(
                "http://evil.example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )

    def test_allows_public_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a public IP is accepted."""
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            _validate_webhook_url(
                "http://public.example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError):
            _validate_webhook_url(
                "ftp://example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )

    def test_rejects_localhost(self, monkeypatch):
        block_private_targets(monkeypatch)
        with pytest.raises(ValueError):
            _validate_webhook_url(
                "http://localhost:8080/callback",
                host_policy=HostPolicy.unrestricted(),
            )


class TestPlaintextWebhookRefusal:
    """#499: the HMAC scheme protects integrity, not confidentiality, so a
    plaintext http:// webhook rides signature AND payload TLS-less. Doors
    refuse it unless the operator opted out."""

    def test_a_plaintext_webhook_url_is_refused(self, monkeypatch):
        with plaintext_webhook_refusal(monkeypatch), pytest.raises(ValueError, match="https://"):
            ensure_https_webhook_url("http://receiver.example/hook")

    def test_an_https_webhook_url_is_accepted(self):
        ensure_https_webhook_url("https://receiver.example/hook")

    def test_the_operator_opt_in_admits_plaintext(self, monkeypatch):
        from app.api.webhooks import reset_plaintext_webhook_memo

        monkeypatch.setenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", "1")
        try:
            ensure_https_webhook_url("http://receiver.example/hook")
        finally:
            reset_plaintext_webhook_memo()

    def test_the_opt_in_warns_once_per_distinct_env_value(self, monkeypatch, caplog):
        """The memo warns once per distinct env value, not per call, and an env
        change (or removal) lands on the very next call (#499 residual)."""
        import logging

        from app.api import webhooks

        webhooks.reset_plaintext_webhook_memo()
        try:
            with caplog.at_level(logging.WARNING, logger="app.api.webhooks"):
                monkeypatch.setenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", "1")
                assert webhooks.allow_plaintext_webhooks() is True
                assert webhooks.allow_plaintext_webhooks() is True
                assert len(caplog.records) == 1, "the memo must warn once, not per call"

                monkeypatch.setenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", "true")
                assert webhooks.allow_plaintext_webhooks() is True
                assert len(caplog.records) == 2, "a changed env value must warn its new decision"

                monkeypatch.delenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK")
                assert webhooks.allow_plaintext_webhooks() is False
                assert len(caplog.records) == 2, "an override must land on the next call"

                monkeypatch.setenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", "1")
                assert webhooks.allow_plaintext_webhooks() is True
                assert len(caplog.records) == 3, "the next distinct value warns again"
        finally:
            webhooks.reset_plaintext_webhook_memo()


class TestWebhookDeliveryPinning:
    async def test_delivery_posts_to_pinned_ip(self, monkeypatch):
        """The outbound POST must target the resolved/pinned IP, not the raw hostname, and
        preserve the original Host header (DNS-rebinding safe), driven through the
        production funnel so the guard protects the path real crawls take.
        """
        delivered = record_webhook_delivery(monkeypatch)
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            await _post_webhook(
                "http://webhook.example.com/cb",
                {"results": []},
                host_policy=HostPolicy.unrestricted(),
            )

        _assert_pinned_delivery(delivered)

    async def test_https_delivery_connects_to_the_validated_ip(self, monkeypatch):
        """HTTPS webhooks now connect to the validated pinned IP like HTTP (DNS-rebinding
        safe); the logical hostname is preserved for TLS SNI via the request extension."""
        from app.api.webhooks import deliver_pinned_webhook

        delivered = record_webhook_delivery(monkeypatch)
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            await deliver_pinned_webhook(
                "https://webhook.example.com/cb",
                b"{}",
                host_policy=HostPolicy.unrestricted(),
            )

        _assert_pinned_delivery(delivered)

    async def test_delivery_response_body_is_capped(self, monkeypatch, caplog):
        """The webhook RESPONSE is attacker-controlled and must never be
        buffered unboundedly. The delivery must consume it through the
        bounded_io cap and survive an oversized body without raising."""
        import logging

        from app.api.webhooks import deliver_pinned_webhook, max_webhook_size

        # ``host_resolves_to`` also stands the private-target escape hatch down, so the rejection this test
        # wants is the policy's answer and not an accident of what the machine's resolver returned.
        big_body = b"x" * (max_webhook_size() + 1)
        delivered = record_webhook_delivery(monkeypatch, body=big_body)
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            with caplog.at_level(logging.WARNING, logger="app.api.webhooks"):
                await deliver_pinned_webhook(
                    "http://webhook.example.com/cb",
                    b"{}",
                    host_policy=HostPolicy.unrestricted(),
                )

        assert delivered, "delivery never ran; the cap was never exercised"
        assert any("webhook_response_too_large" in r.message for r in caplog.records), caplog.text


def test_webhook_size_cap_honors_a_live_env_override(monkeypatch):
    """The cap is read 2-3 times per delivery; caching it on the raw env value parses
    once per distinct value while an override -- operator env, or a test -- is still
    picked up on the very next call.
    """
    from app.api import webhooks

    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "1234")
    assert webhooks.max_webhook_size() == 1234, "the override was not honored"

    # A later change to the same env var is reflected without a reload.
    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "4321")
    assert webhooks.max_webhook_size() == 4321, "a cached value shadowed the new override"

    # Removing the override restores the default, not a stale cached value.
    monkeypatch.delenv("CRAWLUTRON_MAX_WEBHOOK_SIZE")
    assert webhooks.max_webhook_size() == 10_485_760


def test_webhook_size_cap_keeps_the_unit_floor(monkeypatch):
    """The ``lo=1`` floor survives the caching path: a zero/negative cap would
    cap every delivery at nothing and silently drop all webhooks."""
    from app.api import webhooks

    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "0")
    assert webhooks.max_webhook_size() == 1

    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "not-an-int")
    assert webhooks.max_webhook_size() == 10_485_760, "a garbage value must fall back, not raise"
