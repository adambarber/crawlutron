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

    # Over the entry cap: only the first max entries are kept.
    big = {"blob": "x" * 200_000}
    for i in range(60):
        await handler(_FakeResponse(f"https://x/{i}", big))

    assert len(collected) <= 50, f"unbounded collection: {len(collected)}"
    assert all("payload_truncated" in e or (e.get("payload") or {}).get("blob") for e in collected)
    # Oversized payloads are truncated out rather than stored whole.
    assert all(len((e.get("payload") or {}).get("blob", "")) <= 200_000 for e in collected)
    assert any(e.get("payload_truncated") for e in collected)
