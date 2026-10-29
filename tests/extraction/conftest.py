"""Extraction-package fixtures shared by the doc-pool suites.

The autouse root ``_document_pool_per_test`` owns real per-test pool creation;
these are the seeded/stub variants the pool's own unit tests arrange by hand.
"""

from __future__ import annotations

import pytest

import crawlutron.extraction.documents as docex
from tests._process_doubles import stub_worker


@pytest.fixture
def fake_pool(monkeypatch):
    """Stub the production pool's collaborators and restore it afterwards."""
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_DOC_WORKERS", "1")
    saved_pool = docex.DOC_POOL
    try:
        yield monkeypatch
    finally:
        docex.DOC_POOL = saved_pool


@pytest.fixture
def seeded_doc_pool(monkeypatch):
    """A live document worker in the pool, restored afterwards."""
    from tests._doc_pool_fixtures import _seeded

    worker = stub_worker(4242)
    pool, _spawner = _seeded(monkeypatch, worker)
    yield worker, pool
