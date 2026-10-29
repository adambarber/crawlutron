"""The ``client`` fixture: an AsyncClient over this test's own job store."""

import pytest


@pytest.fixture
def client(job_store):
    """An AsyncClient over the jobs app, backed by this test's own ``job_store``.

    Lives here rather than in either test module because both need it; the
    local copy that shadowed the fixture only rewrapped tmp_path."""
    from httpx import ASGITransport, AsyncClient

    from tests._job_harnesses import _make_app

    return AsyncClient(transport=ASGITransport(app=_make_app(job_store)), base_url="http://test")
