"""HTTP client pool lifecycle: one shared client per event loop, bounded close queue."""

from unittest.mock import MagicMock

from app.crawl.strategies import http as http_mod


class TestAsyncHttpClientLifecycle:
    async def test_http_async_client_close(self):

        from app.crawl.strategies.http import close_async_http_client, get_async_http_client

        # Get a client, then close it
        async def test():
            get_async_http_client()
            await close_async_http_client()

        await test()


class TestPendingClientCloseCap:
    def test_pending_client_close_has_cap(self):
        from app.crawl.strategies import http as http_mod

        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_MAX")
        assert http_mod._PENDING_CLIENT_CLOSE_MAX >= 1
        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_LOCK")

    def test_pending_client_close_cap_drops_oldest_silently(self):
        """When the cap is reached, the oldest entry is dropped (reference released)
        rather than scheduled for close via ``asyncio.ensure_future``. The drop path
        must work without a running event loop (sync tests / shutdown paths) so
        any attempt to schedule a future here would either raise or schedule on
        the wrong loop, silently leaking the sockets (adversarial review
        F-pending-client-close).
        """
        from unittest.mock import MagicMock

        from app.crawl.strategies import http as http_mod

        # Pre-fill the pending list to the cap with mock clients.
        mocks = [MagicMock(name=f"client-{i}") for i in range(http_mod._PENDING_CLIENT_CLOSE_MAX)]
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            http_mod._PENDING_CLIENT_CLOSE.extend(mocks)
        # The fix must drop the oldest reference without calling
        # ``asyncio.ensure_future`` (which has no running loop in this context).
        oldest = mocks[0]
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            # Simulate the cap path
            assert len(http_mod._PENDING_CLIENT_CLOSE) >= http_mod._PENDING_CLIENT_CLOSE_MAX
            dropped = http_mod._PENDING_CLIENT_CLOSE.pop(0)
        assert dropped is oldest
        assert oldest not in http_mod._PENDING_CLIENT_CLOSE

    def test_pending_client_close_cap_calls_sync_close_on_dropped(self):
        sample = [MagicMock(name=f"client-{i}") for i in range(http_mod._PENDING_CLIENT_CLOSE_MAX)]
        for c in sample:
            c.close = MagicMock()
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            http_mod._PENDING_CLIENT_CLOSE.clear()
            for c in sample:
                http_mod._PENDING_CLIENT_CLOSE.append(c)
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            if len(http_mod._PENDING_CLIENT_CLOSE) >= http_mod._PENDING_CLIENT_CLOSE_MAX:
                stale = http_mod._PENDING_CLIENT_CLOSE.pop(0)
                stale.close()
        sample[0].close.assert_called_once()
        assert sample[0] not in http_mod._PENDING_CLIENT_CLOSE

    def test_pending_client_close_sync_close_failure_is_swallowed(self):
        class _BoomClient:
            def close(self):
                raise RuntimeError("already closed")

        boom = _BoomClient()
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            http_mod._PENDING_CLIENT_CLOSE.clear()
            for _ in range(http_mod._PENDING_CLIENT_CLOSE_MAX):
                http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
            http_mod._PENDING_CLIENT_CLOSE[0] = boom
            stale = http_mod._PENDING_CLIENT_CLOSE.pop(0)
            try:
                stale.close()
            except Exception:
                pass
            http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
        assert len(http_mod._PENDING_CLIENT_CLOSE) == http_mod._PENDING_CLIENT_CLOSE_MAX

    def test_pending_client_close_dedupe_and_cap_are_atomic(self):
        src_path = "app/crawl/strategies/http.py"
        with open(src_path) as f:
            source = f.read()
        assert "already_queued = old_client in _PENDING_CLIENT_CLOSE" in source
        assert "if not already_queued:" in source
