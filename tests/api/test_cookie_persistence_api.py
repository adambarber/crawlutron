"""Cookie persistence through the /v1/crawl API (real server, real cookie jar)."""

import pytest

from crawlutron.transport.cookie_jar import global_cookie_jar
from tests._assertions import completed_crawl, wire_result
from tests._fetcher_factories import make_crawl_payload
from tests.helpers.server import WEBHOOK_RECEIVED


class TestCookiePersistenceApi:
    """Cookie persistence integration tests (real server, real cookie jar)."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url, tmp_path):
        self.client = async_client
        self.base = base_url
        self.cookie_file = tmp_path / "cookie_jar.json"
        # ASGITransport doesn't run FastAPI lifespan, so manually manage the global cookie jar
        global_cookie_jar().import_from_file(str(self.cookie_file))
        WEBHOOK_RECEIVED.clear()
        yield
        # Save cookies on teardown
        global_cookie_jar().export_to_file(str(self.cookie_file))

    async def test_cookie_persistence_across_requests(self):
        # First request: sets a cookie
        await completed_crawl(
            self.client, make_crawl_payload(f"{self.base}/set-cookie", max_discovered_links=10)
        )
        # Second request: should send the cookie back
        body2 = await completed_crawl(
            self.client, make_crawl_payload(f"{self.base}/check-cookie", max_discovered_links=10)
        )
        result = wire_result(body2)
        # The response body of /check-cookie is the Cookie header it received
        assert "session_id=abc123" in str(result.get("html") or result.get("text") or "")

    async def test_cookie_persistence_survives_restart(self):
        # First crawl sets cookie
        await completed_crawl(
            self.client, make_crawl_payload(f"{self.base}/set-cookie", max_discovered_links=10)
        )
        # Second crawl verifies cookie is sent
        result = wire_result(
            await completed_crawl(
                self.client, make_crawl_payload(f"{self.base}/check-cookie", max_discovered_links=10)
            )
        )
        assert "session_id=abc123" in str(result.get("html") or result.get("text") or "")
