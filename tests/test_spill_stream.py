"""ResultSpill.load_ordered single-pass reassembly contract."""

import fcntl
import inspect
import os
import time
import tracemalloc

from crawlutron.orchestration.engine import ResultSpill
from crawlutron.orchestration.spill_sink import SpillSink
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests._result_builders import Spill, engine_result


class TestTheReassemblyHoldsOneAdmittedSet:
    """The #158 ceiling contract: reassembly RAM is the materialized set -- linear in
    the admitted results, no super-linear overhead -- and the set itself is bounded
    by the crawl's own caps (batch wire cap 500; recursive max_pages cap 2000).
    """

    _N = 200
    _FACTOR = 3  # linear doubles (~2x plus fixed overhead); an O(n^2) reassembly reads ~4x

    def _allocated_peak(self, build) -> int:
        tracemalloc.start()
        built = build()
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert built, "the measured build produced nothing"
        return peak

    async def _reassembly_peak(self, n: int) -> int:
        """tracemalloc peak of one load_ordered over an n-result spill."""
        spill = ResultSpill()
        try:
            for i in range(n):
                await spill.write(i, engine_result(f"https://spill.test/{i}", "rendered"))
            return self._allocated_peak(lambda: spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY))
        finally:
            spill.close()

    async def test_reassembly_peak_grows_linearly_with_the_admitted_set(self):
        peak_n = await self._reassembly_peak(self._N)
        peak_2n = await self._reassembly_peak(2 * self._N)
        assert peak_2n <= self._FACTOR * peak_n, (
            f"reassembly peak at 2n ({peak_2n}) exceeds {self._FACTOR}x the peak at n "
            f"({peak_n}): load_ordered is allocating super-linearly in the admitted set"
        )


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


class TestTheFakeSpillConformsToTheProtocol:
    """The ``Spill`` double is bound where production binds a ``SpillSink``, so its
    surface must be the protocol's surface (#369): a member the protocol declares and
    the double omits is a silent contract drop, invisible until reassembly routes it."""

    def test_every_protocol_member_exists_with_the_protocol_signature(self):
        for member in ("write", "load_ordered", "close"):
            proto = inspect.signature(getattr(SpillSink, member))
            fake = inspect.signature(getattr(Spill, member))
            assert set(proto.parameters) <= set(fake.parameters), (
                f"Spill.{member}{fake} drops the protocol's parameters {proto}: the double"
                " no longer implements the contract it stands in for"
            )

    def test_the_double_answers_the_call_production_makes(self):
        assert Spill().load_ordered(policy=UNRESTRICTED_HOST_POLICY) == []


class TestSpillGcCadence:
    """Tier-2 stale-spill GC is rate-limited, not per-construction: the sweep ran on
    every request; the cadence keeps exactly one guarantee -- the first construction
    always scans, and the next only once the interval has elapsed.
    """

    def _spy(self, monkeypatch):
        import os

        import crawlutron.orchestration.engine as engine_mod

        scans = {"n": 0}
        real_listdir = os.listdir

        def counting_listdir(path):
            scans["n"] += 1
            return real_listdir(path)

        monkeypatch.setattr(engine_mod.os, "listdir", counting_listdir)
        monkeypatch.setattr(engine_mod, "spill_gc_last_run", None)
        return scans

    def test_first_construction_scans_and_following_ones_do_not(self, monkeypatch):
        import crawlutron.orchestration.engine as engine_mod

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

        import crawlutron.orchestration.engine as engine_mod

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
        from crawlutron.orchestration.engine import ResultSpill

        spill = ResultSpill()  # auto-owned path
        path = spill.path
        assert os.path.exists(path)
        spill.close()
        assert not os.path.exists(path), "Auto-owned spill file was not cleaned up"

    def test_result_spill_keeps_caller_owned_path(self):
        """Caller-supplied spill paths are NOT removed on close."""
        import tempfile

        from crawlutron.orchestration.engine import ResultSpill

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
        sweeper = ResultSpill()
        try:
            sweeper.gc_stale_spills(1.0)
            assert not os.path.exists(path), "GC kept a spill nobody owns"
        finally:
            sweeper.close()
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

            sweeper = ResultSpill()
            try:
                sweeper.gc_stale_spills(1.0)
            finally:
                sweeper.close()

            assert os.path.exists(path), "GC unlinked a spill another process owns"
        finally:
            foreign.close()
            if os.path.exists(path):
                os.unlink(path)
