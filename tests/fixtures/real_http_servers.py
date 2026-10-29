"""The real-HTTP-server fixtures: ``counting_server`` and ``etag_server``."""

import pytest


def _served(handler_cls):
    """Serve one handler class on an ephemeral port, then tear it down.

    The teardown is written once: a leaked handler thread outlives the test
    that started it."""
    from tests.helpers.server import spawn_threaded_server

    server = spawn_threaded_server(handler_cls)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def counting_server():
    """Real HTTP server serving one cacheable page; counts hits on ``server.hits``."""
    from tests.helpers.server import CountingHandler

    yield from _served(CountingHandler)


@pytest.fixture
def etag_server():
    """Real HTTP server honoring If-None-Match; counts hits on ``server.hits``.

    Mutate ``helpers.server.EtagHandler.etag`` / ``.body`` to simulate an
    upstream content change.
    """
    from tests.helpers.server import EtagHandler

    yield from _served(EtagHandler)
