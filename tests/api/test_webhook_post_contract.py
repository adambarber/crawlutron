"""post_webhook is the delivery chokepoint: it owns the scheme law itself (#S2)
and swallows serialization failures per its best-effort contract (#S3)."""

import logging

import httpx

from crawlutron.models.host_policy import HostPolicy
from tests._webhook_doubles import record_webhook_delivery

_HTTPS = "https://webhook.example.com/cb"
_OK: dict[str, list[object]] = {"results": []}


async def _post_through_chokepoint(monkeypatch, *, url: str, payload) -> list[httpx.Request]:
    """One post_webhook drive for the contract tests; returns delivered requests."""
    from crawlutron.api import webhooks
    from crawlutron.models.normalized_url import NormalizedUrl
    from crawlutron.models.webhook_target import WebhookTarget

    delivered = record_webhook_delivery(monkeypatch)
    # Built past admission: a credential-shaped query param is refused at the
    # door now; these tests pin the delivery-side redaction behind that law.
    target = WebhookTarget(target=NormalizedUrl(url, policy=HostPolicy.unrestricted()))
    await webhooks.post_webhook(target, payload)
    return delivered


async def _assert_https_delivers_once(monkeypatch) -> None:
    delivered = await _post_through_chokepoint(monkeypatch, url=_HTTPS, payload=_OK)
    assert len(delivered) == 1


class TestPostWebhookSchemeLaw:
    async def _refused(self, monkeypatch, caplog, url):
        """Post a refused non-https URL; return delivered and the log."""
        with caplog.at_level(logging.WARNING, logger="crawlutron.api.webhooks"):
            delivered = await _post_through_chokepoint(monkeypatch, url=url, payload=_OK)
        return delivered

    async def test_an_http_webhook_url_is_refused_without_a_socket(self, monkeypatch, caplog):
        delivered = await self._refused(monkeypatch, caplog, "http://webhook.example.com/cb")
        assert delivered == [], "post_webhook opened a socket to a non-https webhook url"
        assert "webhook_http_refused" in caplog.text

    async def test_the_refusal_log_carries_only_the_origin(self, monkeypatch, caplog):
        """A caller-supplied webhook URL can carry query credentials; the log
        masks the secret value, never the raw URL (#636 via main's #639 law)."""
        await self._refused(monkeypatch, caplog, "http://webhook.example.com/cb?api_key=hunter2")
        record = next(r for r in caplog.records if r.getMessage() == "webhook_http_refused")
        assert "hunter2" not in record.url, record.url
        assert record.url.startswith("http://webhook.example.com"), record.url
        assert "hunter2" not in caplog.text, "the raw webhook URL reached the log"

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
