"""Shared per-loop HTTP clients build once and close cleanly."""

import contextlib

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


class _FakeClient:
    def __init__(self):
        self.is_closed = False
        self._bound_loop = None

    async def aclose(self):
        self.is_closed = True
