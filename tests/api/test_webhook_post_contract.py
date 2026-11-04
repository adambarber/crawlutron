"""post_webhook is the delivery chokepoint: it owns the scheme law itself (#S2)
and swallows serialization failures per its best-effort contract (#S3)."""

import logging

import httpx

from crawlutron.models.host_policy import HostPolicy
from tests._webhook_doubles import record_webhook_delivery

_HTTPS = "https://webhook.example.com/cb"
_OK = {"results": []}


async def _post_through_chokepoint(monkeypatch, *, url: str, payload) -> list[httpx.Request]:
    """One post_webhook drive for the contract tests; returns delivered requests."""
    from crawlutron.api import webhooks

    delivered = record_webhook_delivery(monkeypatch)
    await webhooks.post_webhook(url, payload, host_policy=HostPolicy.unrestricted())
    return delivered


async def _assert_https_delivers_once(monkeypatch) -> None:
    delivered = await _post_through_chokepoint(monkeypatch, url=_HTTPS, payload=_OK)
    assert len(delivered) == 1


class TestPostWebhookSchemeLaw:
    async def test_plaintext_url_is_refused_without_a_socket(self, monkeypatch, caplog):
        from crawlutron.api import webhooks

        monkeypatch.setenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", "0")
        webhooks.reset_plaintext_webhook_memo()
        with caplog.at_level(logging.WARNING, logger="crawlutron.api.webhooks"):
            delivered = await _post_through_chokepoint(
                monkeypatch, url="http://webhook.example.com/cb", payload=_OK
            )
        assert delivered == [], "post_webhook opened a socket to a plaintext http:// url"
        assert "webhook_plaintext_refused" in caplog.text

    async def test_https_url_is_unaffected(self, monkeypatch):
        await _assert_https_delivers_once(monkeypatch)


class TestPostWebhookPayloadSerialization:
    async def test_unserializable_payload_is_logged_not_raised(self, monkeypatch, caplog):
        with caplog.at_level(logging.WARNING, logger="crawlutron.api.webhooks"):
            delivered = await _post_through_chokepoint(monkeypatch, url=_HTTPS, payload={"data": {object()}})
        assert delivered == [], "post_webhook delivered a payload that failed to serialize"
        assert "webhook_payload_serialization_failed" in caplog.text

    async def test_valid_payload_still_delivers(self, monkeypatch):
        await _assert_https_delivers_once(monkeypatch)
