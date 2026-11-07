"""The saturated-gate dispatch floor must batch, not serial (#618).

Under a fully saturated admission gate the non-recursive dispatcher took the
whole remaining batch through a full re-bucket + admit + run cycle for ONE job
per pass -- N sequential cycles for an N-task batch, each awaiting the admit
yield and re-sampling the gate. The floor exists to guarantee forward progress,
not a serial pace: one pass must dispatch a floor-sized chunk.
"""

from crawlutron.orchestration.engine import crawl_request
from crawlutron.runtime.pressure_sizing import WeightedAdmission as Admission
from tests._admission_doubles import page_route, pin_small_box
from tests._fetch_doubles import FetcherDouble as Fetcher

#: The default saturated dispatch floor, documented in .env.example.
FLOOR = 4


class SaturatedAdmission(Admission):
    """The gate at its hard ceiling: both tiers admit nothing, every pass.

    Subclasses the real admission so the double is the production type; the
    saturated pass then rides the dispatch floor instead of the gate verdict.
    """

    def __init__(self, admit_http: int = 0) -> None:
        from crawlutron.runtime.pressure_gates import ResourceGate

        super().__init__(
            ResourceGate(memory_ratio=lambda: 1.0, cpu_ratio=lambda: 1.0),
            _zero_policy(),
        )
        self.admit_http = admit_http
        self.passes = 0

    async def admit(self, want_http: int, want_browser: int) -> tuple[int, int]:
        self.passes += 1
        return min(want_http, self.admit_http), 0


def _zero_policy():
    from crawlutron.runtime.pressure_sizing import AdmissionPolicy

    return AdmissionPolicy(
        http_task_bytes=0,
        browser_task_bytes=0,
        browser_pool_reserve_bytes=0,
        max_http_concurrency=0,
        max_browser_concurrency=0,
    )


async def _run_batch(monkeypatch, count: int, admission: Admission):
    pin_small_box(monkeypatch)
    # Install the double by type: dispatch builds its admission through
    # _build_admission, which constructs Admission directly.
    monkeypatch.setattr(
        "crawlutron.orchestration.request_pipeline.WeightedAdmission",
        lambda gate, policy: admission,
    )
    fetcher = Fetcher(page_route)
    urls = [f"http://site{i}.example/page" for i in range(count)]
    out = await crawl_request(
        {"urls": urls, "policy": {"limits": {"max_depth": 0}, "strategies": ["HTTP_DECLARED_CRAWLER"]}},
        http_fetcher=fetcher,
        cffi_fetcher=fetcher,
        browser_worker=None,
    )
    return out


async def test_saturated_floor_dispatches_floor_sized_chunks(monkeypatch):
    """A saturated gate with a floor of 4 drains a 12-job batch in 3 passes."""
    admission = SaturatedAdmission()
    out = await _run_batch(monkeypatch, 12, admission)
    assert len(out["results"]) == 12, "every submitted page must return a result"
    expected = -(-12 // FLOOR)
    assert admission.passes == expected, (
        f"the saturated gate took {admission.passes} admit passes for 12 jobs "
        f"(expected {expected}): one re-bucket + admit cycle per job is the #618 amplification"
    )


async def test_saturated_floor_scales_with_the_batch(monkeypatch):
    """Bigger batch, same floor: the pass count grows sub-linearly, not 1:1."""
    admission = SaturatedAdmission()
    out = await _run_batch(monkeypatch, 24, admission)
    assert len(out["results"]) == 24
    assert admission.passes == -(-24 // FLOOR), (
        f"the saturated gate took {admission.passes} passes for 24 jobs: the floor serial"
    )


async def test_gate_admitted_dispatch_never_takes_the_floor(monkeypatch):
    """When the gate admits, dispatch follows the gate verdict, not the floor."""
    admission = SaturatedAdmission(admit_http=2)
    out = await _run_batch(monkeypatch, 6, admission)
    assert len(out["results"]) == 6
    assert admission.passes == 3, f"gate-admitted dispatch took the floor: {admission.passes} passes"
