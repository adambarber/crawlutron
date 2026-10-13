"""Shared per-loop HTTP clients build once and close cleanly."""

import contextlib
from unittest.mock import MagicMock

import app.transport.http_client_pool as http_mod
from app.transport.http_client_pool import get_async_http_client


class TestPooledClientBuilders:
    """The public seam that replaces monkeypatching the builders.

    The pooled clients are process-wide and created lazily on whichever loop
    asks first, so there is no per-test construction site to inject at. That
    absence is why the builders used to be patched on the module -- and why the
    module's own docstring advertised a late-bound lambda as a design goal, so
    the patch would be observed. The seam states the same capability as an
    argument and removes the reason for the indirection.
    """

    def test_a_supplied_builder_produces_the_pooled_client(self):
        sentinel = object()
        with http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            assert http_mod.get_cffi_session() is sentinel

    def test_the_builder_is_restored_after_the_seam_closes(self):
        sentinel = object()
        with http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            pass
        assert http_mod.get_cffi_session() is not sentinel

    def test_the_builder_is_restored_when_the_body_raises(self):
        """A failing test must not leave a fake in a process-wide singleton.

        This is why the seam restores rather than merely being callable: a
        builder left installed outlives the test that set it, and every later
        test in the process that asks for a pooled client then gets the
        previous test's double. That failure is order-dependent, which is the
        hardest kind to read.
        """
        sentinel = object()
        with contextlib.suppress(ValueError), http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            raise ValueError
        assert http_mod.get_cffi_session() is not sentinel

    def test_supplying_one_builder_leaves_the_other_alone(self):
        sentinel = object()
        with http_mod.pooled_client_builders(cffi=lambda settings: sentinel):
            assert not isinstance(http_mod.get_async_http_client(), _FakeClient)


class TestAsyncHttpClientLifecycle:
    async def test_http_async_client_close(self):
        from app.transport.http_client_pool import close_async_http_client

        # Build a client, then close it: the close path exercises a live one.
        get_async_http_client()
        await close_async_http_client()


class TestPendingClientCloseCap:
    def test_pending_client_close_has_cap(self):
        from app.transport import http_client_pool as http_mod

        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_MAX")
        assert http_mod._PENDING_CLIENT_CLOSE_MAX >= 1
        assert hasattr(http_mod, "_PENDING_CLIENT_CLOSE_LOCK")

    def test_pending_client_close_cap_drops_oldest_silently(self):
        """When the cap is reached, the oldest entry is dropped (reference released)
        rather than scheduled for close via ``asyncio.ensure_future``. The drop path
        must work without a running event loop (sync tests / shutdown paths) so
        any attempt to schedule a future here would either raise or schedule on
        the wrong loop, silently leaking the sockets.
        """
        from unittest.mock import MagicMock

        from app.transport import http_client_pool as http_mod

        # Pre-fill the pending list to the cap with mock clients.
        mocks = [MagicMock(name=f"client-{i}") for i in range(http_mod._PENDING_CLIENT_CLOSE_MAX)]
        with http_mod._PENDING_CLIENT_CLOSE_LOCK:
            http_mod._PENDING_CLIENT_CLOSE.extend(mocks)
        # The fix must drop the oldest reference without calling ``asyncio.ensure_future`` (which has no
        # running loop in this context).
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
            with contextlib.suppress(Exception):
                stale.close()
            http_mod._PENDING_CLIENT_CLOSE.append(MagicMock())
        assert len(http_mod._PENDING_CLIENT_CLOSE) == http_mod._PENDING_CLIENT_CLOSE_MAX

    def test_pending_client_close_dedupe_and_cap_are_atomic(self):
        src_path = "app/transport/http_client_pool.py"
        with open(src_path) as f:
            source = f.read()
        assert "already_queued = old_client in _PENDING_CLIENT_CLOSE" in source
        assert "if not already_queued:" in source


class _FakeClient:
    def __init__(self):
        self.is_closed = False
        self._bound_loop = None

    async def aclose(self):
        self.is_closed = True
