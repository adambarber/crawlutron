"""N10: lifespan installs the readiness utilization sampler at startup and
uninstalls it at shutdown (symmetry with M11's wiring).

Also the shutdown sequence's own contract: every teardown step is attempted,
so one pool whose close() raises cannot cost the others theirs.
"""


def _spy_on_every_teardown(monkeypatch, http_closer) -> list[str]:
    """Record the order the lifespan teardown reaches each pool in.

    The resource gate is the real object with its real ``close`` replaced, not a
    stand-in: startup drives the same instance, and a hand-rolled gate that
    only knows the one method under test fails on whatever the next one is.
    """
    from app import main as main_mod
    from app.api import job_runner as job_runner_mod
    from app.crawl import engine as engine_mod
    from app.crawl import extraction as extraction_mod
    from app.crawl.strategies import http_client_pool as http_mod

    ran: list[str] = []

    def _record(name):
        async def _close():
            ran.append(name)

        return _close

    async def _noop_job_shutdown():
        return None

    monkeypatch.setattr(job_runner_mod, "shutdown_job_runner", _noop_job_shutdown)
    monkeypatch.setattr(http_mod, "close_async_http_client", http_closer)
    monkeypatch.setattr(http_mod, "close_cffi_session", _record("cffi"))
    monkeypatch.setattr(main_mod, "close_browser_pool", _record("browser"))
    monkeypatch.setattr(extraction_mod, "_shutdown_doc_extraction_executor", _record("doc"))
    monkeypatch.setattr(engine_mod._resource_gate, "close", lambda: ran.append("gate"))
    return ran


class TestLifespanSamplerSymmetry:
    async def test_shutdown_uninstalls_utilization_sampler(self, monkeypatch):
        from app import main as main_mod
        from app.crawl.strategies import http_client_pool as http_mod
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


class TestShutdownRunsEveryStep:
    """A failing close() must not skip the teardown that follows it.

    The document-extraction subprocesses are the reason this is not
    theoretical: nothing touches them after the lifespan block, so the step
    that skips them is the step that leaks every markitdown worker's memory
    and pipes past the process's own shutdown -- which is what the comment
    beside that call says it is there to prevent.
    """

    async def test_a_raising_closer_does_not_skip_later_pools(self, monkeypatch):
        import pytest

        from app import main as main_mod

        async def _explode():
            raise RuntimeError("pool close failed")

        ran = _spy_on_every_teardown(monkeypatch, _explode)

        with pytest.raises(RuntimeError, match="shutdown steps failed"):
            async with main_mod.app.router.lifespan_context(main_mod.app):
                pass

        skipped = [n for n in ("cffi", "browser", "doc", "gate") if n not in ran]
        assert not skipped, f"every teardown step must be attempted; skipped: {skipped}"

    async def test_a_clean_shutdown_runs_every_step_and_raises_nothing(self, monkeypatch):
        from app import main as main_mod

        async def _close():
            return None

        ran = _spy_on_every_teardown(monkeypatch, _close)

        async with main_mod.app.router.lifespan_context(main_mod.app):
            pass

        assert ran == ["cffi", "browser", "doc", "gate"]
