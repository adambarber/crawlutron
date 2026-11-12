"""Extraction test harnesses shared across the extraction test modules.

Declared support surface (#476): moved verbatim from test_extraction.py / test_doc_pool.py so a sibling
test module imports these from here, not from another test's internals.
"""

from __future__ import annotations

from crawlutron.extraction.doc_pool import DocExtractionPool
from tests._process_doubles import StubSpawner, stub_worker

__all__ = ["acquire_the_replacement", "assert_extraction_empty"]


def assert_extraction_empty(result) -> None:
    """A failed document extraction yields nothing at all, in both fields.

    A timeout, crash or parse failure all claim "no text and no markdown";
    asserting one would let a half-filled result through as a success.
    """
    assert result.text is None
    assert result.markdown is None


def acquire_the_replacement(pool: DocExtractionPool, spawner: StubSpawner, pid: int, bound: float = 0.3):
    """Acquire from a pool whose only slots are unusable; report the spawn too.

    Three tests ask the same grow-past-a-bad-slot question; the short bound is
    part of the arrangement, not a detail. Returns ``(spawned, worker)``."""
    spawner.hand_out(stub_worker(pid))
    worker = pool.acquire(bound)
    return spawner.asked, worker
