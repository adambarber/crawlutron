"""#599 (redaction half): credentialed URLs never reach a log line.

The chokepoints are the objects: ``WebhookTarget`` admits (refusing userinfo)
and projects redacted everywhere; ``redact_url`` is its internal mechanism.
Every webhook log path emits ``target.redacted()``, proven here.
"""

from __future__ import annotations

import logging

import pytest

from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.safety.url_redaction import redact_url

SECRET_QUERY_URLS = [
    "https://example.com/hook?key=secret",
    "https://example.com/hook?token=abc&x=y",
    "https://example.com/hook?api_key=z&ok=1",
]


@pytest.mark.parametrize("url", SECRET_QUERY_URLS, ids=lambda u: u[:34])
def test_redact_url_masks_secret_query_values(url):
    redacted = redact_url(url)
    assert "secret" not in redacted and "abc" not in redacted and "z&ok" not in redacted
    assert "example.com" in redacted


def test_redact_url_masks_userinfo():
    redacted = redact_url("https://user:pass@example.com/hook")
    assert "pass" not in redacted and "user:" not in redacted and "example.com/hook" in redacted


def test_a_clean_url_survives_unchanged():
    assert redact_url("https://example.com/hook") == "https://example.com/hook"


def test_a_malformed_url_returns_fully_masked():
    """An unparseable URL is exactly the shape whose credentials hide from
    parsing, so the log form is a placeholder, never the raw text."""
    assert redact_url("not a url at all") == "<unparseable-url>"


def test_the_target_refuses_userinfo_at_the_constructor():
    with pytest.raises(ValueError, match="userinfo_refused"):
        WebhookTarget.admit("https://user:pass@example.com/hook", host_policy=HostPolicy.unrestricted())


def test_the_target_masks_secret_query_values_in_every_projection():
    target = WebhookTarget.admit("https://example.com/hook?key=secret", host_policy=HostPolicy.unrestricted())
    assert "secret" not in target.redacted()
    assert "secret" not in str(target)
    assert "secret" not in repr(target)
    assert target.delivery_url() == "https://example.com/hook?key=secret"


def test_the_target_serializes_redacted():
    target = WebhookTarget.admit("https://example.com/hook?key=secret", host_policy=HostPolicy.unrestricted())
    dumped = target.model_dump_json() if hasattr(target, "model_dump_json") else str(target)
    assert "secret" not in dumped


def test_watchstate_serializes_and_reprs_the_webhook_redacted():
    from crawlutron.models.watch_state import WatchState

    watch = WatchState(
        watch_id="w1",
        url="https://example.com/",
        webhook_url="https://example.com/hook?key=secret",
        interval_seconds=60,
        next_check_at=1.0,
        created_at=0.0,
        updated_at=0.0,
    )
    assert "secret" not in watch.model_dump_json()
    assert "secret" not in repr(watch)
    assert "secret" not in str(watch.model_dump())
    assert watch.delivery_url() == "https://example.com/hook?key=secret"


async def test_webhook_log_paths_never_emit_a_secret(caplog, monkeypatch):
    from crawlutron.api import webhooks

    target = WebhookTarget.admit("https://example.com/hook?key=secret", host_policy=HostPolicy.unrestricted())
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", "0")
    webhooks.reset_plaintext_webhook_memo()
    monkeypatch.setattr(webhooks, "webhook_max_attempts", lambda: 1)
    monkeypatch.setattr(webhooks, "webhook_backoff_base_ms", lambda: 0)
    with caplog.at_level(logging.WARNING, logger="crawlutron.api.webhooks"):
        # Plaintext-refusal path logs the redacted URL.
        await webhooks.post_webhook(
            WebhookTarget.admit("http://example.com/hook?key=secret", host_policy=HostPolicy.unrestricted()),
            {},
        )
        # Serialization-failure path.
        await webhooks.post_webhook(target, {"data": {object()}})
        # Oversized-payload path: a tiny cap forces the too-large log line.
        monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", "1")
        await webhooks.post_webhook(target, {})
        # Delivery-failure path: the receiver refuses permanently.
        from tests._webhook_doubles import record_webhook_delivery

        record_webhook_delivery(monkeypatch, status_code=400)
        await webhooks.post_webhook(target, {})
    for record in caplog.records:
        assert "secret" not in str(record.__dict__), record.getMessage()
