"""The ``test_server`` fixture: one real in-process HTTP server for the session."""

import pytest


@pytest.fixture(scope="session")
def test_server():
    """Start a real in-process HTTP server once per test session."""
    from tests.helpers.server import spawn_server

    server = spawn_server()
    yield server
    server.stop()
