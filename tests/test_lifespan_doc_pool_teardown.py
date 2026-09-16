"""L1: lifespan shutdown must retire the persistent document workers.

``_shutdown_doc_extraction_executor`` existed but was only wired into the
session-scoped test fixture (``conftest._cleanup_extraction_executors``), never
into the production lifespan. The browser pool, the async http client and the
cffi session are all closed at shutdown; the document-extraction subprocess pool
was the orphan, so a production shutdown leaked every markitdown worker (and its
memory and pipes) for the life of the process.
"""

import pytest


class _StubProc:
    """``multiprocessing.Process`` stand-in that records its shutdown."""

    def __init__(self) -> None:
        self._alive = True
        self.signals: list[str] = []

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.signals.append("SIGTERM")
        self._alive = False

    def kill(self) -> None:
        self.signals.append("SIGKILL")
        self._alive = False

    def join(self, timeout=None) -> None:  # noqa: ARG002 - matches the real API
        return None

    @property
    def exitcode(self) -> int | None:
        return None if self._alive else -15

    @property
    def terminated(self) -> bool:
        return bool(self.signals)


class _StubConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


async def _noop() -> None:
    return None


@pytest.fixture
def seeded_doc_pool(monkeypatch):
    """A live document worker in the pool, restored afterwards."""
    from app.crawl import extraction as ext_mod

    worker = ext_mod._DocWorker(_StubProc(), _StubConn())
    saved_pool = ext_mod._DOC_POOL
    with ext_mod._POOL_LOCK:
        ext_mod._DOC_POOL = [worker]
    try:
        yield worker
    finally:
        with ext_mod._POOL_LOCK:
            ext_mod._DOC_POOL = saved_pool


class TestLifespanDocPoolTeardown:
    @pytest.mark.usefixtures("real_sleeps")
    async def test_shutdown_retires_the_document_workers(self, monkeypatch, seeded_doc_pool):
        """The document pool must be torn down by the production lifespan.

        Asserted by effect, not by spying: a live worker is seeded into the pool
        before the app runs, and shutdown must retire it and drop the pool. The
        heavy client teardowns are stubbed only so the test does not start real
        services; the document teardown itself runs for real.
        """
        from app import main as main_mod
        from app.crawl import extraction as ext_mod
        from app.crawl.strategies import http as http_mod
        from app.health import readiness as readiness_mod
        from app.health.readiness import ReadinessState

        fresh = ReadinessState()
        monkeypatch.setattr(readiness_mod, "READINESS_STATE", fresh)
        monkeypatch.setattr(http_mod, "close_async_http_client", _noop)
        monkeypatch.setattr(http_mod, "close_cffi_session", _noop)
        monkeypatch.setattr(main_mod, "close_browser_pool", _noop)

        worker = seeded_doc_pool

        async with main_mod.app.router.lifespan_context(main_mod.app):
            # Startup does not touch the document pool, and the worker must
            # still be live inside the app: the teardown is a shutdown concern.
            assert worker.proc.terminated is False, "startup retired a document worker"
            with ext_mod._POOL_LOCK:
                assert ext_mod._DOC_POOL == [worker]

        assert worker.proc.terminated, (
            "lifespan shutdown did not retire the document worker; the pool was "
            "torn down only by the test fixture"
        )
        assert worker.conn.closed, "shutdown left the worker's pipe open"
        with ext_mod._POOL_LOCK:
            assert ext_mod._DOC_POOL is None, "the document pool survived shutdown"
