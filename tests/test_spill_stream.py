"""M10: _ResultSpill.load_ordered single-pass reassembly contract."""

import inspect
import json

from _testkit import os

from app.crawl.engine import _ResultSpill


class TestLoadOrderedContract:
    """M10: load_ordered assembles results in one pass, in submission order."""

    def test_shuffled_writes_reassemble_in_submission_order(self):
        spill = _ResultSpill()
        try:
            for i in (5, 0, 3, 1, 4, 2):
                spill._fh.write(json.dumps({"i": i, "r": {"n": i}}) + "\n")
            spill.count = 6
            assert spill.load_ordered() == [{"n": i} for i in range(6)]
        finally:
            spill.close()

    def test_sparse_indices_keep_every_result_in_position(self):
        spill = _ResultSpill()
        try:
            for i in (0, 2, 9):
                spill._fh.write(json.dumps({"i": i, "r": {"n": i}}) + "\n")
            spill.count = 3
            out = spill.load_ordered()
            assert [r["n"] for r in out] == [0, 2, 9]
        finally:
            spill.close()

    def test_empty_spill_returns_empty_list(self):
        spill = _ResultSpill()
        try:
            assert spill.load_ordered() == []
        finally:
            spill.close()

    def test_single_pass_no_tuple_rows_or_full_sort(self):
        src = inspect.getsource(_ResultSpill.load_ordered)
        assert "rows" not in src
        assert ".sort(" not in src


class TestSpillGcCadence:
    """Tier-2 T5: stale-spill GC is rate-limited, not per-construction.

    The listdir+getmtime sweep of the system temp dir ran on EVERY crawl
    request. The cadence gate keeps exactly one guarantee shape: the first
    construction in a process always scans (so bc-spill-* files orphaned by a
    killed run get reclaimed), and the next scan happens only once the
    interval has elapsed.
    """

    def _spy(self, monkeypatch):
        import os

        import app.crawl.engine as engine_mod

        scans = {"n": 0}
        real_listdir = os.listdir

        def counting_listdir(path):
            scans["n"] += 1
            return real_listdir(path)

        monkeypatch.setattr(engine_mod.os, "listdir", counting_listdir)
        monkeypatch.setattr(engine_mod, "_LAST_SPILL_GC_MONOTONIC", None)
        return scans

    def test_first_construction_scans_and_following_ones_do_not(self, monkeypatch):
        import app.crawl.engine as engine_mod

        scans = self._spy(monkeypatch)
        spills = [_ResultSpill(), _ResultSpill(), _ResultSpill()]
        try:
            assert scans["n"] == 1
        finally:
            for spill in spills:
                spill.close()
        assert engine_mod._LAST_SPILL_GC_MONOTONIC is not None

    def test_scan_resumes_once_the_interval_elapsed(self, monkeypatch):
        import time

        import app.crawl.engine as engine_mod

        scans = self._spy(monkeypatch)
        spill = _ResultSpill()
        assert scans["n"] == 1
        monkeypatch.setattr(
            engine_mod,
            "_LAST_SPILL_GC_MONOTONIC",
            time.monotonic() - (engine_mod._SPILL_GC_MIN_INTERVAL_SECONDS + 1),
        )
        spill2 = _ResultSpill()
        try:
            assert scans["n"] == 2
        finally:
            spill.close()
            spill2.close()


class TestSpillFileOwnership:
    def test_result_spill_close_deletes_owned_file(self):
        """Auto-created spill files are removed on close."""
        from app.crawl.engine import _ResultSpill

        spill = _ResultSpill()  # auto-owned path
        path = spill.path
        assert os.path.exists(path)
        spill.close()
        assert not os.path.exists(path), "Auto-owned spill file was not cleaned up"

    def test_result_spill_keeps_caller_owned_path(self):
        """Caller-supplied spill paths are NOT removed on close."""
        import tempfile

        from app.crawl.engine import _ResultSpill

        target = os.path.join(tempfile.gettempdir(), "spill_keep_test.json")
        try:
            spill = _ResultSpill(target)
            spill.close()
            assert os.path.exists(target), "Caller-owned spill file was wrongly deleted"
        finally:
            if os.path.exists(target):
                os.unlink(target)
