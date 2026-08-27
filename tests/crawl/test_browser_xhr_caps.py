import pytest

pytestmark = pytest.mark.browser

"""XHR/fetch interception must be bounded (hostile pages fire endless JSON)."""


from app.crawl.strategies.browser import BrowserRenderer



class _FakeResponse:
    def __init__(self, url, payload):
        self.url = url
        self.headers = {"content-type": "application/json"}
        self.status = 200
        self._payload = payload

    async def json(self):
        return self._payload

async def test_response_handler_caps_entries_and_payload_size():
    collected = []
    handler = BrowserRenderer._make_response_handler(collected)

    # Over the entry cap: only the first max_entries (10) are kept.
    big = {"blob": "x" * 200_000}
    for i in range(60):
        await handler(_FakeResponse(f"https://x/{i}", big))

    assert len(collected) == 10, f"entry cap not enforced: {len(collected)}"
    # Each kept payload is capped at max_payload_chars (8192); oversized ones
    # are flagged truncated rather than stored whole.
    assert all(
        e.get("payload_truncated")
        or len((e.get("payload") or {}).get("blob", "")) <= 8192
        for e in collected
    )
    assert all("payload_truncated" in e or (e.get("payload") or {}).get("blob") for e in collected)
    # Oversized payloads are truncated out rather than stored whole.
    assert all(len((e.get("payload") or {}).get("blob", "")) <= 200_000 for e in collected)
    assert any(e.get("payload_truncated") for e in collected)

