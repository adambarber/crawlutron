"""ResultSpill.load_ordered single-pass reassembly contract."""

import fcntl
import inspect
import json
import os
import time

from app.models.serialization import to_dict
from app.orchestration.engine import ResultSpill
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests._result_builders import engine_result


def _spilled(index: int) -> str:
    """One real spill line: ``to_dict`` of a real result, under ``ResultSpill``.

    The spill's contract is that what ``write`` serialized comes back equal, and
    ``load_ordered`` rebuilds through ``from_wire``. A toy payload cannot
    exercise that -- it failed the rebuild rather than testing the ordering --
    so the line is built from the production pair.
    """
    return json.dumps({"i": index, "r": to_dict(engine_result(f"https://spill.test/{index}", "rendered"))})


class TestLoadOrderedContract:
    """Load_ordered assembles results in one pass, in submission order."""

    def test_shuffled_writes_reassemble_in_submission_order(self):
        spill = ResultSpill()
        try:
            for i in (5, 0, 3, 1, 4, 2):
                spill._fh.write(_spilled(i) + "\n")
            spill.count = 6
            assert [r.original_url for r in spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY)] == [
                f"https://spill.test/{i}" for i in range(6)
            ]
        finally:
            spill.close()

    def test_sparse_indices_keep_every_result_in_position(self):
        spill = ResultSpill()
        try:
            for i in (0, 2, 9):
                spill._fh.write(_spilled(i) + "\n")
            spill.count = 3
            out = spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY)
            assert [r.original_url for r in out] == [
                "https://spill.test/0",
                "https://spill.test/2",
                "https://spill.test/9",
            ]
        finally:
            spill.close()

    def test_empty_spill_returns_empty_list(self):
        spill = ResultSpill()
        try:
            assert spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY) == []
        finally:
            spill.close()

    def test_single_pass_no_tuple_rows_or_full_sort(self):
        src = inspect.getsource(ResultSpill.load_ordered)
        assert "rows" not in src
        assert ".sort(" not in src


class TestSpillGcCadence:
    """Tier-2 stale-spill GC is rate-limited, not per-construction.

    The listdir+getmtime sweep of the system temp dir ran on EVERY crawl
    request. The cadence gate keeps exactly one guarantee shape: the first
    construction in a process always scans (so bc-spill-* files orphaned by a
    killed run get reclaimed), and the next scan happens only once the
    interval has elapsed.
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
    """A live spill is the crawl's result, not a checkpoint that can be dropped.

    ``load_ordered`` reassembles the result by opening the spill's PATH by name.
    An unlink from a concurrent GC sweep therefore does not cost a worker its
    local checkpoint -- it makes the finished crawl's own output unreadable, and
    the whole crawl has already been paid for by then. A whole-site job that
    runs past ``gc_age_seconds`` is ordinary, not exotic, so the sweep has to
    treat "still in use" as a hard skip rather than as a tolerable loss.
    """

    @staticmethod
    def _age(path: str, seconds: float) -> None:
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_a_live_spill_survives_a_sweep_that_would_otherwise_collect_it(self):
        spill = ResultSpill()
        try:
            spill._fh.write(_spilled(0) + "\n")
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

    def test_the_sweep_skips_only_the_spills_that_are_actually_in_use(self):
        live = ResultSpill()
        abandoned = ResultSpill()
        live_path, abandoned_path = live.path, abandoned.path
        live._fh.write(_spilled(0) + "\n")
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
        """Ownership is a kernel fact, not a fact about this process's memory.

        The in-process live set could only answer "is a spill owned by *me*
        live?", so a concurrent process's in-flight spill was indistinguishable
        from one abandoned by a killed run and got unlinked mid-crawl. The whole
        temp dir is shared, so that is the default outcome whenever two
        processes crawl at once -- a dev server beside a test run, or any
        future second worker.

        A distinct open file description holding the lock reproduces exactly
        the kernel state a second process creates, without spawning one.
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
