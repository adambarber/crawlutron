import app.extraction.documents as docex
from app.orchestration.doc_pool import DocExtractionPool, DocWorker
from tests._process_doubles import StubSpawner


def fresh_doc_pool(monkeypatch, *, spawn=None) -> DocExtractionPool:
    """A fresh, isolated real pool installed over both module bindings, so a test's
    extraction (documents.DOC_POOL) and its health assertions (content's re-export) agree."""
    import app.extraction.content as ext_mod
    import app.orchestration.doc_pool as doc_pool_mod

    pool = DocExtractionPool(
        spawn=spawn or doc_pool_mod.spawn_doc_worker,
        worker_count=docex.doc_worker_count,
        enabled=lambda: True,
    )
    monkeypatch.setattr(docex, "DOC_POOL", pool)
    monkeypatch.setattr(ext_mod, "DOC_POOL", pool)
    return pool


def _stub_pool(*, spawn, worker_count: int = 1, lock=None) -> DocExtractionPool:
    """A pool of stub workers: one worker wide unless told otherwise."""
    return DocExtractionPool(
        spawn=spawn,
        worker_count=lambda: worker_count,
        enabled=lambda: True,
        lock=lock,
    )


def _seeded(monkeypatch, *workers: DocWorker, lock=None) -> tuple[DocExtractionPool, StubSpawner]:
    """A pool already holding ``workers``, arrived at by a real acquisition.

    Seeding through the pool's own acquire means the test cannot assert
    against an arrangement production would never produce."""
    spawner = StubSpawner(*workers)
    pool = _stub_pool(spawn=spawner, worker_count=len(workers), lock=lock)
    held = [pool.acquire(0.1) for _ in workers]
    assert held == list(workers), "the pool grew slots in an order no test asked for"
    for worker in held:
        pool.release(worker)
    spawner.asked.clear()
    monkeypatch.setattr(docex, "DOC_POOL", pool)
    return pool, spawner
