"""N10: lifespan installs the readiness utilization sampler at startup and
uninstalls it at shutdown (symmetry with M11's wiring)."""

import pytest


class TestLifespanSamplerSymmetry:
    @pytest.mark.usefixtures("real_sleeps")
    async def test_shutdown_uninstalls_utilization_sampler(self, monkeypatch):
        from app import main as main_mod
        from app.crawl.strategies import http as http_mod
        from app.health import readiness as readiness_mod
        from app.health.readiness import ReadinessState

        fresh = ReadinessState()
        monkeypatch.setattr(readiness_mod, "READINESS_STATE", fresh)

        async def _noop_client_close():
            return None

        async def _noop_pool_close():
            return None

        monkeypatch.setattr(http_mod, "close_async_http_client", _noop_client_close)
        monkeypatch.setattr(main_mod, "close_browser_pool", _noop_pool_close)

        async with main_mod.app.router.lifespan_context(main_mod.app):
            assert fresh._utilization_sampler is not None, "startup must install the sampler"

        assert fresh._utilization_sampler is None, "shutdown must uninstall the sampler"
