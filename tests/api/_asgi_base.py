"""The ASGI endpoint base: client + base_url fixtures shared across API tests."""

import pytest

from tests.helpers.server import WEBHOOK_RECEIVED


class _AsgiEndpointTests:
    """The ASGI client plus base URL; a copy of this autouse fixture is a rule
    with two owners -- one that forgot ``WEBHOOK_RECEIVED.clear()`` read another
    test's delivery. A class with a different need keeps its own fixture.
    """

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()
