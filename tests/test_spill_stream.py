"""ResultSpill.load_ordered single-pass reassembly contract."""

import fcntl
import os
import time

from app.orchestration.engine import ResultSpill
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests._result_builders import engine_result


class TestLoadOrderedContract:
    """Load_ordered assembles results in submission order, in one pass."""

    async def test_shuffled_writes_reassemble_in_submission_order(self):
        spill = ResultSpill()
        try:
            for i in (5, 0, 3, 1, 4, 2):
                await spill.write(i, engine_result(f"https://spill.test/{i}", "rendered"))
            assert [r.original_url for r in spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY)] == [
                f"https://spill.test/{i}" for i in range(6)
            ]
        finally:
            spill.close()

    async def test_sparse_indices_keep_every_result_in_position(self):
        spill = ResultSpill()
        try:
            for i in (0, 2, 9):
                await spill.write(i, engine_result(f"https://spill.test/{i}", "rendered"))
            out = spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY)
            assert [r.original_url for r in out] == [
                "https://spill.test/0",
                "https://spill.test/2",
                "https://spill.test/9",
            ]
        finally:
            spill.close()

    async def test_empty_spill_returns_empty_list(self):
        spill = ResultSpill()
        try:
            assert spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY) == []
        finally:
            spill.close()


class TestSpillGcCadence:
    """Tier-2 stale-spill GC is rate-limited, not per-construction: the sweep ran on
    every request; the cadence keeps exactly one guarantee -- the first construction
    always scans, and the next only once the interval has elapsed.
    """

    def _spy(self, monkeypatch):
        import os

        import app.orchestration.engine as engine_mod

        scans = {"n": 0}
        real_listdir = os.listdir

        def counting_listdir(path):
            scans["n"] += 1
            return real_listdir(path)

        monkeypatch.setattr(engine_mod.os, "listdir", counting_listdir)
        monkeypatch.setattr(engine_mod, "spill_gc_last_run", None)
        return scans

    def test_first_construction_scans_and_following_ones_do_not(self, monkeypatch):
        import app.orchestration.engine as engine_mod

        scans = self._spy(monkeypatch)
        spills = [ResultSpill(), ResultSpill(), ResultSpill()]
        try:
            assert scans["n"] == 1
        finally:
            for spill in spills:
                spill.close()
        assert engine_mod.spill_gc_last_run is not None

    def test_scan_resumes_once_the_interval_elapsed(self, monkeypatch):
        import time

        import app.orchestration.engine as engine_mod

        scans = self._spy(monkeypatch)
        spill = ResultSpill()
        assert scans["n"] == 1
        monkeypatch.setattr(
            engine_mod,
            "spill_gc_last_run",
            time.monotonic() - (engine_mod.SPILL_GC_MIN_INTERVAL_SECONDS + 1),
        )
        spill2 = ResultSpill()
        try:
            assert scans["n"] == 2
        finally:
            spill.close()
            spill2.close()


class TestSpillFileOwnership:
    def test_result_spill_close_deletes_owned_file(self):
        """Auto-created spill files are removed on close."""
        from app.orchestration.engine import ResultSpill

        spill = ResultSpill()  # auto-owned path
        path = spill.path
        assert os.path.exists(path)
        spill.close()
        assert not os.path.exists(path), "Auto-owned spill file was not cleaned up"

    def test_result_spill_keeps_caller_owned_path(self):
        """Caller-supplied spill paths are NOT removed on close."""
        import tempfile

        from app.orchestration.engine import ResultSpill

        target = os.path.join(tempfile.gettempdir(), "spill_keep_test.json")
        try:
            spill = ResultSpill(target)
            spill.close()
            assert os.path.exists(target), "Caller-owned spill file was wrongly deleted"
        finally:
            if os.path.exists(target):
                os.unlink(target)


class TestGcNeverUnlinksASpillStillInUse:
    """A live spill is the crawl's result, not a droppable checkpoint: ``load_ordered``
    reassembles output by opening the spill's PATH, so a concurrent unlink makes the
    finished crawl unreadable -- still-in-use must be a hard skip, not a tolerable loss.
    """

    @staticmethod
    def _age(path: str, seconds: float) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old))

    async def test_a_live_spill_survives_a_sweep_that_would_otherwise_collect_it(self):
        spill = ResultSpill()
        try:
            await spill.write(0, engine_result("https://spill.test/0", "rendered"))
            self._age(spill.path, 7200)

            spill.gc_stale_spills(1.0)

            assert os.path.exists(spill.path), "GC unlinked a spill still in use"
            assert [r.original_url for r in spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY)] == [
                "https://spill.test/0"
            ]
        finally:
            spill.close()

    def test_an_abandoned_spill_is_still_reclaimed(self):
        spill = ResultSpill()
        path = spill.path
        spill.close()
        # Close() unlinks an owned spill, so recreate one to stand in for a spill abandoned by a worker that
        # died without closing it.
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"i":0}\n')
        self._age(path, 7200)
        try:
            ResultSpill().gc_stale_spills(1.0)
            assert not os.path.exists(path), "GC kept a spill nobody owns"
        finally:
            if os.path.exists(path):
                os.unlink(path)

    async def test_the_sweep_skips_only_the_spills_that_are_actually_in_use(self):
        live = ResultSpill()
        abandoned = ResultSpill()
        live_path, abandoned_path = live.path, abandoned.path
        await live.write(0, engine_result("https://spill.test/0", "rendered"))
        abandoned.close()
        with open(abandoned_path, "w", encoding="utf-8") as handle:
            handle.write('{"i":0}\n')
        self._age(live_path, 7200)
        self._age(abandoned_path, 7200)
        try:
            live.gc_stale_spills(1.0)
            assert os.path.exists(live_path), "a live spill was collected"
            assert not os.path.exists(abandoned_path), "an abandoned spill was kept"
        finally:
            live.close()
            if os.path.exists(abandoned_path):
                os.unlink(abandoned_path)

    def test_a_spill_owned_by_another_process_is_not_collected(self):
        """Ownership is a kernel fact, not a process-memory fact: the in-process live set
        missed a concurrent process's in-flight spill, which got unlinked mid-crawl; a
        distinct open file description reproduces the kernel state without spawning a second process.
        """
        seed = ResultSpill()
        path = seed.path
        seed.close()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"i":0}\n')
        foreign = open(path, "r+", encoding="utf-8")
        try:
            fcntl.flock(foreign.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._age(path, 7200)

            ResultSpill().gc_stale_spills(1.0)

            assert os.path.exists(path), "GC unlinked a spill another process owns"
        finally:
            foreign.close()
            if os.path.exists(path):
                os.unlink(path)
