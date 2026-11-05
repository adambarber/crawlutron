"""The zero-admission dispatch pass must yield, not hot-spin the gates (#526)."""

import asyncio

from crawlutron.orchestration.admission_stall import AdmissionStallGuard
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_sizing import AdmissionPolicy, WeightedAdmission
from tests._recursive_harnesses import drive_recursive_crawl, make_crawl_task


async def _allows(url):
    return True


class _ZeroGate(ResourceGate):
    """A gate reporting zero use; the saturated override consults none of it,
    but the surface must be the real ResourceGate, not a duck."""

    def memory_ratio(self) -> float:
        return 0.0

    def cpu_ratio(self) -> float:
        return 0.0

    def batch_scale_factor(self) -> float:
        return 1.0

    async def wait_while_saturated(self, max_wait_seconds: float | None = None) -> None:
        raise AssertionError("the saturated admission never waits on the gate")


class SaturatedNeverAwaiting(WeightedAdmission):
    """(0,0) every pass, without ever awaiting: the ONLY yield must be the loop's.

    Subclasses the real admission so the double is structurally the production
    type, not a duck it merely resembles; the zero policy is what makes it."""

    def __init__(self, stop_event: asyncio.Event) -> None:
        super().__init__(
            _ZeroGate(),
            AdmissionPolicy(
                http_task_bytes=0,
                browser_task_bytes=0,
                browser_pool_reserve_bytes=0,
                max_http_concurrency=0,
                max_browser_concurrency=0,
            ),
        )
        self.calls = 0
        self._stop = stop_event

    async def admit(self, want_http: int, want_browser: int) -> tuple[int, int]:
        self.calls += 1
        if self.calls >= 3:
            self._stop.set()
        return 0, 0


async def test_zero_admission_pass_yields_instead_of_hot_spinning(monkeypatch):
    """A pass that admits nothing, while not yet stalled, must yield to the loop.

    The stall deadline exists to STOP the loop; until it fires the loop still
    owes the event loop a turn per pass instead of hot gate-sampling (#526)."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep_spy(delay=None):
        delays.append(delay)
        return await real_sleep(delay)

    monkeypatch.setattr("crawlutron.orchestration.admission_stall.asyncio.sleep", sleep_spy)
    stop = asyncio.Event()

    async def worker(job):
        raise AssertionError(f"nothing dispatches under a (0,0) admission: {job}")

    admission = SaturatedNeverAwaiting(stop)
    await drive_recursive_crawl(
        [make_crawl_task(0, "a"), make_crawl_task(1, "a")],
        worker,
        allows=_allows,
        admission=admission,
        stall_guard=AdmissionStallGuard(clock=lambda: 0.0, no_progress_seconds=60.0),
        shutdown_event=stop,
    )

    assert admission.calls == 3, "the shutdown hand-off never reached the loop"
    assert len(delays) == admission.calls, (
        "the zero-admission passes did not yield between admissions: the loop "
        f"hot-sampled the gates {admission.calls} times with no sleep request"
    )
