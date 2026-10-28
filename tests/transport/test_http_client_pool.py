"""Shared per-loop HTTP clients build once and close cleanly."""

import contextlib

import app.transport.http_client_pool as http_mod
from app.transport.http_client_pool import get_async_http_client
from tests._pool_ws_doubles import FakeClient as _FakeClient


class TestPooledClientBuilders:
    """The public seam that replaces monkeypatching the builders.

    The pooled clients are process-wide and lazily created, so there was no
    per-test site to inject at -- the seam states the capability as an argument.
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

        A builder left installed outlives the test, so every later test gets the
        previous one's double -- an order-dependent, hard-to-read failure.
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
