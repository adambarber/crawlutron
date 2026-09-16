"""HTTP client pool lifecycle: one shared client per event loop, bounded close queue."""


class TestAsyncHttpClientLifecycle:
    async def test_http_async_client_close(self):

        from app.crawl.strategies.http import close_async_http_client, get_async_http_client

        # Get a client, then close it
        async def test():
            get_async_http_client()
            await close_async_http_client()

        await test()
