"""curl_cffi streaming and impersonation behavior: shared session, handle release, caps."""


class TestCffiImpersonateStrategy:
    async def test_http_cffi_imersonate_result(self):
        from app.crawl.cache import HttpCache
        from app.crawl.strategies.http import _http_strategy_result

        def cffi_fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><body>content</body></html>"
                final_url = url
                elapsed_ms = 10

            return Resp()

        result = await _http_strategy_result(
            original_url="http://x/",
            normalized_url="http://x/",
            strategy="HTTP_CFFI_IMPERSONATE",
            policy={
                "strategies": ["HTTP_CFFI_IMPERSONATE"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            },
            http_fetcher=cffi_fetcher,
            cache=HttpCache(),
        )
        assert result.classification == "rendered"


class TestCffiFetcherLiveFetch:
    async def test_http_cffi_import_fallback(self, real_sleeps):
        # Live impersonated fetch: needs real wall-clock sleeps -- the suite's
        # instant-asyncio.sleep patch starves curl_cffi's socket/timer loop
        # and wedges the transfer past all client timeouts.
        from helpers.server import spawn_server

        from app.crawl.strategies.http import _default_cffi_fetcher

        server = spawn_server()
        try:
            result = await _default_cffi_fetcher(server.base_url + "/", {}, 10, 50 * 1024 * 1024)
            assert result.status_code == 200
        finally:
            server.stop()
