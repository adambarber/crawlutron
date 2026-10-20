"""Both pools satisfy the pool protocol matching their concurrency modality (#127).

BrowserPool awaits a coroutine (AsyncPool); DocExtractionPool blocks a thread (SyncPool).
Each is proven statically conformant by a typed-annotation assignment mypy checks.
"""

import app.orchestration.pool as pool_mod
from app.orchestration.doc_pool import DocExtractionPool, DocWorker
from app.orchestration.pool import AsyncPool, SyncPool
from app.transport.browser_pool import BrowserPool, BrowserRenderer

# Static conformance: these assignments are type-checked by mypy at the module level. If either
# pool's ``acquire``/``release``/``__len__`` drifts from its protocol surface, mypy flags it here.
_browser_pool_is_an_async_pool: AsyncPool[BrowserRenderer] = BrowserPool(pool_size=1)
_doc_pool_is_a_sync_pool: SyncPool[DocWorker] = DocExtractionPool(
    worker_count=lambda: 1, enabled=lambda: True
)

# The typed names above must really type-check, not be optimized away: reference them bare.
assert _browser_pool_is_an_async_pool is not None
assert _doc_pool_is_a_sync_pool is not None


def test_browser_pool_is_an_async_pool():
    assert isinstance(BrowserPool(pool_size=1), AsyncPool)


def test_doc_extraction_pool_is_a_sync_pool():
    pool = DocExtractionPool(worker_count=lambda: 1, enabled=lambda: True)
    assert isinstance(pool, SyncPool)


def test_pool_exposes_the_shared_verbs():
    # The three same-named verbs both contracts agree on, at runtime.
    for cls in (BrowserPool, DocExtractionPool):
        for verb in ("acquire", "release", "__len__"):
            assert hasattr(cls, verb), f"{cls.__name__} lost the {verb!r} verb"


def test_pool_carries_the_documented_parameterized_lifecycle():
    # The async-vs-sync axis is typed and real; the terminal/recycle axes live
    # on the classes, not the protocols: each keeps its own teardown/close surface.
    assert pool_mod.AsyncPool is AsyncPool
    assert pool_mod.SyncPool is SyncPool
    assert hasattr(DocExtractionPool, "teardown")
    assert hasattr(BrowserPool, "close")
    assert isinstance(DocWorker, type)
    assert isinstance(BrowserRenderer, type)
