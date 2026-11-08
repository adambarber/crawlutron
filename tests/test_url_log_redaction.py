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

# Probe-derived (#659): every name that leaked verbatim through the
# exact-name vocabulary into redacted projections.
AGED_SECRET_QUERY_URLS = [
    "https://example.com/hook?client_secret=s1",
    "https://example.com/hook?access_token=t1",
    "https://example.com/hook?pwd=p1",
    "https://example.com/hook?session=s%3As1",
    "https://example.com/hook?id_token=i1",
]


@pytest.mark.parametrize("url", AGED_SECRET_QUERY_URLS, ids=lambda u: u.split("?")[1])
def test_redact_url_masks_the_aged_vocabulary(url):
    name, _, value = url.split("?")[1].partition("=")
    redacted = redact_url(url)
    assert value not in redacted, f"{name} leaked verbatim: {redacted}"


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


BYPASS_SHAPES = [
    "https://example.com/hook?ke%79=secret",
    "https://example.com/hook?key%20=secret",
    "https://example.com/hook?key[]=secret",
    "https://example.com/hook?a=1;key=secret",
]


def test_redact_url_decodes_names_before_the_secret_check():
    """Percent-encoded and bracketed names are the same secret parameter on the
    wire; the membership test must run on the decoded name, not the raw text."""
    for url in BYPASS_SHAPES[:3]:
        assert "secret" not in redact_url(url), url


def test_redact_url_treats_semicolon_pairs_as_separators():
    """Browsers historically split ``;``-pairs like ``&``; a secret behind a
    semicolon is still a secret in the log form."""
    assert "secret" not in redact_url(BYPASS_SHAPES[3])


def test_redact_url_drops_the_fragment_entirely():
    """A fragment's contents never legitimately appear in a log form: the
    client never sends it, so anything there is attacker- or secret-carried."""
    redacted = redact_url("https://example.com/hook#token=secret")
    assert "secret" not in redacted and "token" not in redacted


def test_the_target_masks_every_bypass_shape():
    """The object's projection inherits the same decoded-name law."""
    for url in BYPASS_SHAPES:
        target = WebhookTarget.admit(url, host_policy=HostPolicy.unrestricted())
        assert "secret" not in target.redacted(), url
        assert "secret" not in str(target), url


def test_watchstate_serialization_masks_every_bypass_shape():
    from crawlutron.models.watch_state import WatchState

    for url in BYPASS_SHAPES:
        watch = WatchState(
            watch_id="w1",
            url="https://example.com/",
            webhook_url=url,
            interval_seconds=60,
            next_check_at=1.0,
            created_at=0.0,
            updated_at=0.0,
        )
        assert "secret" not in watch.model_dump_json(), url


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
    monkeypatch.setattr(webhooks, "webhook_max_attempts", lambda: 1)
    monkeypatch.setattr(webhooks, "webhook_backoff_base_ms", lambda: 0)
    with caplog.at_level(logging.WARNING, logger="crawlutron.api.webhooks"):
        # Non-https refusal path logs the redacted URL.
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


def test_redaction_vocabulary_covers_credential_secret_fields():
    """#659: the redaction vocabulary and Credential's secret field names are
    one opinion -- every field Credential admits as a SecretStr is masked by
    name in the redaction chokepoint, so the two cannot diverge silently."""
    from typing import get_args

    from pydantic import SecretStr

    from crawlutron.models.credential import Credential
    from crawlutron.safety.url_redaction import is_secret_name

    for name, field in Credential.model_fields.items():
        candidates = (field.annotation, *get_args(field.annotation))
        is_secret = any(isinstance(a, type) and issubclass(a, SecretStr) for a in candidates)
        if not is_secret:
            continue
        assert is_secret_name(name), f"Credential secret field {name!r} is not redaction vocabulary"
