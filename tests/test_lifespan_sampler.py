"""Lifespan installs the readiness utilization sampler at startup and uninstalls
it at shutdown; the shutdown contract is that every teardown step is attempted,
so one pool whose close() raises cannot cost the others theirs.
"""

from tests._app_factory import service_app
from tests._lifespan_doubles import stub_heavy_teardown


def _spy_on_every_teardown(monkeypatch, http_closer) -> list[str]:
    """Record the order the lifespan teardown reaches each pool in: the resource gate
    is the real object with its real ``close`` replaced, not a stand-in, so a
    hand-rolled gate that knows one method fails on whatever the next one is.
    """
    from crawlutron import main as main_mod
    from crawlutron.extraction import content as extraction_mod
    from crawlutron.orchestration import engine as engine_mod
    from crawlutron.transport import http_client_pool as http_mod

    ran: list[str] = []

    def _record(name):
        async def _close():
            ran.append(name)

        return _close

    # Start from the neutraliser: the set of closers the lifespan reaches is production's list, and this test
    # only over-runs the four it is counting.
    stub_heavy_teardown(monkeypatch)
    monkeypatch.setattr(http_mod, "close_async_http_client", http_closer)
    monkeypatch.setattr(http_mod, "close_cffi_session", _record("cffi"))
    monkeypatch.setattr(main_mod, "close_browser_pool", _record("browser"))
    monkeypatch.setattr(extraction_mod, "shutdown_doc_extraction_executor", _record("doc"))
    monkeypatch.setattr(engine_mod.resource_gate(), "close", lambda: ran.append("gate"))
    return ran


class TestLifespanSamplerSymmetry:
    async def test_shutdown_uninstalls_utilization_sampler(self, monkeypatch):

        fresh = stub_heavy_teardown(monkeypatch)
        app = service_app()

        async with app.router.lifespan_context(app):
            assert fresh._utilization_sampler is not None, "startup must install the sampler"

        assert fresh._utilization_sampler is None, "shutdown must uninstall the sampler"


class TestShutdownRunsEveryStep:
    """A failing close() must not skip the teardown that follows it: the document
    subprocesses are why this is not theoretical -- the step that skips them leaks
    every markitdown worker's memory and pipes past the process shutdown.
    """

    async def test_a_raising_closer_does_not_skip_later_pools(self, monkeypatch):
        import pytest

        async def _explode():
            raise RuntimeError("pool close failed")

        ran = _spy_on_every_teardown(monkeypatch, _explode)
        app = service_app()

        with pytest.raises(RuntimeError, match="shutdown steps failed"):
            async with app.router.lifespan_context(app):
                pass

        skipped = [n for n in ("cffi", "browser", "doc", "gate") if n not in ran]
        assert not skipped, f"every teardown step must be attempted; skipped: {skipped}"

    async def test_a_clean_shutdown_runs_every_step_and_raises_nothing(self, monkeypatch):

        async def _close():
            return None

        ran = _spy_on_every_teardown(monkeypatch, _close)
        app = service_app()

        async with app.router.lifespan_context(app):
            pass

        assert ran == ["cffi", "browser", "doc", "gate"]
