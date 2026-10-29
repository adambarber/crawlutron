"""The ``base_url`` fixture: the session test server's origin."""

import pytest


@pytest.fixture
def base_url(test_server):
    """The base URL of the session-scoped test server."""
    return test_server.base_url
