"""The ASGI endpoint base: client + base_url fixtures shared across API tests."""

import pytest

from tests.helpers.server import WEBHOOK_RECEIVED


class _AsgiEndpointTests:
    """The ASGI client plus the fixture-server base URL, for endpoint classes.

    Four of the endpoint classes below opened with this identical autouse
    fixture, and a copy of it is a rule with two owners: a class that forgot the
    ``WEBHOOK_RECEIVED.clear()`` inherited a webhook assertion reading another
    test's delivery. It lives here once, and a class that needs a different
    fixture (no base URL, or one that also persists a cookie jar) keeps its own
    rather than inheriting a superset it does not want.
    """

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()
