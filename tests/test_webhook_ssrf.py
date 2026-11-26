"""Webhook SSRF: admission checks shape only; the proxy owns target DNS and the
sole dial. Delivery carries the logical host and preserves the original header.
"""

import pytest

from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.service.webhooks import (
    ensure_https_webhook_url,
)
from crawlutron.service.webhooks import (
    post_webhook as _post_webhook,
)
from crawlutron.service.webhooks import (
    validate_webhook_url_async as _validate_webhook_url,
)
from safe_url.host_policy import HostPolicy
from tests._assertions import (
    block_private_targets,
    host_resolves_to,
)
from tests._webhook_doubles import assert_delivered_once, record_webhook_delivery


def _assert_logical_delivery(delivered) -> None:
    """Delivery carries the logical host only; no resolved address reaches the wire."""
    assert_delivered_once(delivered)
    assert str(delivered[0].url) == "https://webhook.example.com/cb", delivered
    assert delivered[0].headers.get("Host") == "webhook.example.com", delivered


class TestWebhookUrlValidation:
    async def test_admits_shape_without_target_dns(self, monkeypatch):
        """Admission never resolves the target; the proxy makes that decision."""

        def refuse(*args, **kwargs):
            raise AssertionError("webhook admission must not resolve its target")

        monkeypatch.setattr("socket.getaddrinfo", refuse)
        await _validate_webhook_url(
            "http://evil.example.com/callback",
            host_policy=HostPolicy.unrestricted(),
        )

    async def test_accepts_public_hostname_without_resolution(self, monkeypatch):
        """A public hostname is admitted by shape, not by what DNS answers."""
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("app DNS")),
        )
        await _validate_webhook_url(
            "http://public.example.com/callback",
            host_policy=HostPolicy.unrestricted(),
        )

    async def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError):
            await _validate_webhook_url(
                "ftp://example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )

    async def test_rejects_localhost(self, monkeypatch):
        block_private_targets(monkeypatch)
        with pytest.raises(ValueError):
            await _validate_webhook_url(
                "http://localhost:8080/callback",
                host_policy=HostPolicy.unrestricted(),
            )


class TestHttpsOnlyWebhookRefusal:
    """#651: the HMAC scheme protects integrity, not confidentiality, so a
    webhook callback is an https-only hop -- no configuration surface."""

    def test_an_http_webhook_url_is_refused(self):
        """Refused unconditionally (#651), with or without any env var set."""
        with pytest.raises(ValueError, match="https://"):
            ensure_https_webhook_url("http://receiver.example/hook")

    def test_an_https_webhook_url_is_accepted(self):
        ensure_https_webhook_url("https://receiver.example/hook")

    @pytest.mark.parametrize(
        "scheme_url",
        [
            "ftp://example.com/hook",
            "ws://example.com/hook",
            "wss://example.com/hook",
        ],
    )
    def test_ensure_https_webhook_url_refuses_non_https_schemes(self, scheme_url):
        """ftp://, ws:// and wss:// are unencrypted and must be refused like
        http://, not passed through to delivery (#958)."""
        with pytest.raises(ValueError, match="https://"):
            ensure_https_webhook_url(scheme_url)


class TestWebhookDeliveryStaysLogical:
    async def test_delivery_posts_to_the_logical_host(self, monkeypatch):
        """The outbound POST carries the logical host, never a resolved IP, and
        preserves the original Host header -- driven through the production funnel.
        """
        delivered = record_webhook_delivery(monkeypatch)
        await _post_webhook(
            WebhookTarget.admit("https://webhook.example.com/cb", host_policy=HostPolicy.unrestricted()),
            {"results": []},
        )

        _assert_logical_delivery(delivered)

    async def test_https_delivery_keeps_the_logical_host_for_sni(self, monkeypatch):
        """HTTPS delivery preserves the logical hostname for TLS SNI at the proxy."""
        from crawlutron.service.webhooks import deliver_pinned_webhook

        delivered = record_webhook_delivery(monkeypatch)
        await deliver_pinned_webhook(
            "https://webhook.example.com/cb",
            b"{}",
            host_policy=HostPolicy.unrestricted(),
        )

        _assert_logical_delivery(delivered)

    async def test_delivery_response_body_is_capped(self, monkeypatch, caplog):
        """The webhook RESPONSE is attacker-controlled and must never be
        buffered unboundedly. The delivery must consume it through the
        bounded_io cap and survive an oversized body without raising."""
        import logging

        from crawlutron.service.webhooks import deliver_pinned_webhook, max_webhook_size

        # ``host_resolves_to`` also stands the private-target escape hatch down, so the rejection this test
        # wants is the policy's answer and not an accident of what the machine's resolver returned.
        big_body = b"x" * (max_webhook_size() + 1)
        delivered = record_webhook_delivery(monkeypatch, body=big_body)
        with host_resolves_to(monkeypatch, "8.8.8.8"):
            with caplog.at_level(logging.WARNING, logger="crawlutron.service.webhooks"):
                await deliver_pinned_webhook(
                    "https://webhook.example.com/cb",
                    b"{}",
                    host_policy=HostPolicy.unrestricted(),
                )

        assert delivered, "delivery never ran; the cap was never exercised"
        assert any("webhook_response_too_large" in r.message for r in caplog.records), caplog.text


def test_webhook_size_cap_honors_a_live_env_override(monkeypatch):
    """The cap comes from the settings home; an env change is honored after a re-parse."""
    from crawlutron.config.settings_cache import reset_cached_settings
    from crawlutron.service import webhooks

    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "1234")
    reset_cached_settings()
    assert webhooks.max_webhook_size() == 1234, "the override was not honored"

    # A later change is picked up through the settings re-parse.
    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "4321")
    reset_cached_settings()
    assert webhooks.max_webhook_size() == 4321, "a cached value shadowed the new override"

    # Removing the override restores the default, not a stale cached value.
    monkeypatch.delenv("CRAWLUTRON_MAX_WEBHOOK_SIZE")
    reset_cached_settings()
    assert webhooks.max_webhook_size() == 10_485_760


def test_webhook_size_cap_keeps_the_unit_floor(monkeypatch):
    """The ``lo=1`` floor survives the settings path: a zero cap drops no webhooks."""
    from crawlutron.config.settings_cache import reset_cached_settings
    from crawlutron.service import webhooks

    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "0")
    reset_cached_settings()
    assert webhooks.max_webhook_size() == 1

    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "not-an-int")
    reset_cached_settings()
    assert webhooks.max_webhook_size() == 10_485_760, "a garbage value must fall back, not raise"


async def _deliver_once(monkeypatch, *, secret: str | None):
    """One real delivery against the mocked transport, with the secret set or not."""
    from crawlutron.service.webhooks import deliver_pinned_webhook

    if secret is None:
        monkeypatch.delenv("CRAWLUTRON_WEBHOOK_SECRET", raising=False)
    else:
        monkeypatch.setenv("CRAWLUTRON_WEBHOOK_SECRET", secret)
    record_webhook_delivery(monkeypatch)
    await deliver_pinned_webhook(
        "https://webhook.example.com/cb",
        b"{}",
        host_policy=HostPolicy.unrestricted(),
    )


class TestWebhookNoSecretWarning:
    """#655: an unsigned delivery must say so -- a one-time WARNING naming the
    env var, at the first unsigned delivery; delivery itself stays best-effort."""

    async def test_first_unsigned_delivery_warns_once_naming_the_env_var(self, monkeypatch, caplog):
        import logging

        from crawlutron.runtime import process_state

        process_state.reset_all_process_state()
        with caplog.at_level(logging.WARNING, logger="crawlutron.service.webhooks"):
            await _deliver_once(monkeypatch, secret=None)
            await _deliver_once(monkeypatch, secret=None)

        warnings = [r for r in caplog.records if "webhook_unsigned_delivery" in r.message]
        assert len(warnings) == 1, f"expected exactly one one-time warning, saw {len(warnings)}"
        assert "CRAWLUTRON_WEBHOOK_SECRET" in warnings[0].getMessage()

    async def test_signed_delivery_does_not_warn(self, monkeypatch, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="crawlutron.service.webhooks"):
            await _deliver_once(monkeypatch, secret="test-secret")

        assert not [r for r in caplog.records if "webhook_unsigned_delivery" in r.message]
