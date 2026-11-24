"""A lifespan test double releases the pools it stands in for."""

from tests._app_factory import service_app
from tests._lifespan_doubles import stub_heavy_teardown


async def test_lifespan_teardown_closes_the_live_cffi_session(monkeypatch):
    from crawlutron.transport import pooled_cffi

    stub_heavy_teardown(monkeypatch)
    app = service_app()

    async with app.router.lifespan_context(app):
        pooled_cffi.get_cffi_session()
        assert pooled_cffi.owns_process_state(), "precondition: a real session is live"

    assert not pooled_cffi.owns_process_state(), "the cffi session survived lifespan teardown"
