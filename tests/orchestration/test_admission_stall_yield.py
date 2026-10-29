"""The zero-admission dispatch pass must yield, not hot-spin the gates (#526)."""

import asyncio

from crawlutron.orchestration.admission_stall import AdmissionStallGuard
from tests._recursive_harnesses import drive_recursive_crawl, make_crawl_task


async def _allows(url):
    return True


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

    class SaturatedNeverAwaiting:
        """(0,0) every pass, without ever awaiting: the ONLY yield must be the loop's."""

        def __init__(self):
            self.calls = 0

        async def admit(self, want_http, want_browser):
            self.calls += 1
            if self.calls >= 3:
                stop.set()
            return 0, 0

    async def worker(job):
        raise AssertionError(f"nothing dispatches under a (0,0) admission: {job}")

    admission = SaturatedNeverAwaiting()
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
