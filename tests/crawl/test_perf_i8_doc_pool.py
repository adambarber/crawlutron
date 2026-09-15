"""Perf item 8: markitdown extraction must use a persistent process pool.

``_extract_document`` spawns a fresh ``multiprocessing.Process`` per document
(process spawn + a markitdown import per page). Workers must persist across
documents (importing markitdown once) while keeping timeout-kill semantics,
and the hermetic TEST_MARKITDOWN_* seam must keep working.
"""


def _counted_spawns(monkeypatch):
    """Count pool worker constructions parent-side (spawn-safe: no pickling)."""
    import app.crawl.extraction as ext_mod

    real_spawn = ext_mod._spawn_doc_worker
    constructions = 0

    def counting_spawn():
        nonlocal constructions
        constructions += 1
        return real_spawn()

    monkeypatch.setattr(ext_mod, "_spawn_doc_worker", counting_spawn)
    return lambda: constructions


def _use_single_worker(monkeypatch):
    import app.crawl.extraction as ext_mod

    monkeypatch.setenv("BROWSER_CRAWLER_EXTRACTION_DOC_WORKERS", "1")
    monkeypatch.setenv("BROWSER_CRAWLER_TEST_HOOKS", "1")
    monkeypatch.setenv("TEST_MARKITDOWN_OUTPUT", "## Hello Markdown")
    ext_mod._reset_doc_pool()


def _restore_pool():
    import app.crawl.extraction as ext_mod

    for var in ("TEST_MARKITDOWN_OUTPUT", "TEST_MARKITDOWN_HANG", "TEST_MARKITDOWN_DIE"):
        ext_mod.os.environ.pop(var, None)
    ext_mod.os.environ.pop("BROWSER_CRAWLER_TEST_HOOKS", None)
    ext_mod.os.environ.pop("BROWSER_CRAWLER_EXTRACTION_DOC_WORKERS", None)
    ext_mod._reset_doc_pool()


def test_doc_workers_persist_across_documents(monkeypatch):
    """N documents cost one worker construction, not N spawns."""
    import app.crawl.extraction as ext_mod

    constructions = _counted_spawns(monkeypatch)
    _use_single_worker(monkeypatch)
    try:
        for _ in range(3):
            result = ext_mod._extract_document(b"%PDF-1.4 fake", "http://example.com/x.pdf")
            assert result.markdown == "## Hello Markdown"
        assert constructions() == 1
        assert len(ext_mod._doc_pool_pids()) == 1
    finally:
        _restore_pool()


def test_doc_worker_pids_stable_across_tasks(monkeypatch):
    """The same worker serves consecutive documents (no respawn churn)."""
    import app.crawl.extraction as ext_mod

    _use_single_worker(monkeypatch)
    try:
        ext_mod._extract_document(b"%PDF-1.4 fake", "http://example.com/a.pdf")
        first = ext_mod._doc_pool_pids()
        ext_mod._extract_document(b"%PDF-1.4 fake", "http://example.com/b.pdf")
        second = ext_mod._doc_pool_pids()
        assert first == second
        assert all(pid > 0 for pid in first)
    finally:
        _restore_pool()


def test_doc_timeout_kills_and_pool_recovers(monkeypatch):
    """A hung worker is killed on timeout; the pool serves the next document."""
    import app.crawl.extraction as ext_mod

    constructions = _counted_spawns(monkeypatch)
    _use_single_worker(monkeypatch)
    monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 0.05)
    try:
        import time

        monkeypatch.setenv("TEST_MARKITDOWN_HANG", "1")
        started = time.monotonic()
        result = ext_mod._extract_document(b"%PDF-1.4 fake", "http://example.com/hang.pdf")
        assert time.monotonic() - started < 5.0
        assert result.text is None and result.markdown is None
        hung_pids = ext_mod._doc_pool_pids()

        monkeypatch.delenv("TEST_MARKITDOWN_HANG")
        # Normal timeout for recovery: the respawned worker must boot (spawn
        # platforms) before the task timeout starts mattering again.
        monkeypatch.setattr(ext_mod, "_EXTRACTION_DOC_TIMEOUT", 10.0)
        recovered = ext_mod._extract_document(b"%PDF-1.4 fake", "http://example.com/ok.pdf")
        assert recovered.markdown == "## Hello Markdown"
        # The hung worker was killed and its slot respawned: exactly one
        # replacement construction, pool back at one live worker.
        assert constructions() == 2
        live_pids = ext_mod._doc_pool_pids()
        assert len(live_pids) == 1
        assert live_pids != hung_pids
    finally:
        _restore_pool()
