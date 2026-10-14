import app.extraction.documents as docex
from app.orchestration.doc_pool import DocExtractionPool, DocWorker
from tests._process_doubles import StubSpawner


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
