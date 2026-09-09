"""XHR/ajax interception edge cases in the browser worker."""

from coverage_helpers import *


class TestBrowserXhrEdgeCases:
    async def test_json_payload_captured(self):
        from app.crawl.strategies.browser import BrowserRenderer

        collected: list[dict[str, Any]] = []
        handler = BrowserRenderer._make_response_handler(collected)

        class FakeResponse:
            headers = {"content-type": "application/json"}
            url = "http://x/api/data"
            status = 200

            async def json(self):
                return {"a": 1}

        await handler(FakeResponse())
        assert collected[0]["payload"] == {"a": 1}

    async def test_bad_json_payload_captured_as_none(self):
        from app.crawl.strategies.browser import BrowserRenderer

        collected: list[dict[str, Any]] = []
        handler = BrowserRenderer._make_response_handler(collected)

        class FakeResponse:
            headers = {"content-type": "application/json"}
            url = "http://x/api/badjson"
            status = 200

            async def json(self):
                raise ValueError("bad json")

        await handler(FakeResponse())
        assert len(collected) == 1
        assert (collected[0]["payload"]) is None
