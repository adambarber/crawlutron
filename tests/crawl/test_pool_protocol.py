"""Both pools satisfy the one shared Pool protocol (#127)."""

import app.orchestration.pool as pool_mod
from app.orchestration.doc_pool import DocExtractionPool, DocWorker
from app.orchestration.pool import Pool
from app.transport.browser_pool import BrowserPool, BrowserRenderer


def test_browser_pool_is_a_pool():
    assert isinstance(BrowserPool(pool_size=1), Pool)


def test_doc_extraction_pool_is_a_pool():
    pool = DocExtractionPool(worker_count=lambda: 1, enabled=lambda: True)
    assert isinstance(pool, Pool)


def test_pool_exposes_the_shared_verbs():
    # The three same-named verbs both contracts agree on, at runtime.
    for cls in (BrowserPool, DocExtractionPool):
        for verb in ("acquire", "release", "__len__"):
            assert hasattr(cls, verb), f"{cls.__name__} lost the {verb!r} verb"


def test_pool_carries_the_documented_parameterized_lifecycle():
    # The async/sync and terminal/recycle axes live on the classes, not the
    # protocol: each keeps its own teardown/close surface.
    assert pool_mod.Pool is Pool
    assert hasattr(DocExtractionPool, "teardown")
    assert hasattr(BrowserPool, "close")
    assert isinstance(BrowserPool, type)
    assert isinstance(DocWorker, type)
    assert isinstance(BrowserRenderer, type)
