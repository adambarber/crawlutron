"""The sync (non-recursive) batch path must pace itself through the same
RAM-weighted admission gate the recursive path uses -- not run RAM-blind
against a 256-connection hardcode (#350)."""

from __future__ import annotations

import pytest

from admission import AdmissionGate, LaneId, Want
from crawlutron.orchestration import request_pipeline
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_sizing import AdmissionPolicy, TaskRamBudget
from tests._admission_doubles import run_sync_batch


class _RecordingGate:
    """Delegates to a real kernel gate, recording each admit's tier wants."""

    def __init__(self, admits: list[tuple[int, int]], real_gate: AdmissionGate) -> None:
        self._real = real_gate
        self._admits = admits

    def admit(self, want: Want):
        self._admits.append((want.lanes[LaneId.GENERAL].value, want.lanes[LaneId.HEAVY].value))
        return self._real.admit(want)


async def _admission_spy(monkeypatch):
    """Wrap the gate the sync path builds and capture each admit's want, via the
    public ``request_pipeline.build_admission_gate`` door -- no private name."""
    admits: list[tuple[int, int]] = []
    original_build = request_pipeline.build_admission_gate

    def spy_build(
        gate: ResourceGate, policy: AdmissionPolicy, budget: TaskRamBudget | None = None
    ) -> _RecordingGate:
        real = original_build(gate, policy, budget)
        return _RecordingGate(admits, real)

    monkeypatch.setattr(request_pipeline, "build_admission_gate", spy_build)
    return admits


@pytest.mark.parametrize(
    ("policy", "want_http_must_be_zero"),
    [
        # Default chain contains a browser strategy -> unpinned jobs are
        # admitted against browser capacity ((0, N)), never HTTP ((N, 0)).
        ({"limits": {"max_depth": 0}}, True),
        # Chain with no browser link -> admitted as HTTP ((N, 0)).
        ({"limits": {"max_depth": 0}, "strategies": ["HTTP_DECLARED_CRAWLER"]}, False),
    ],
)
async def test_nonrecursive_admission_charge_matches_policy_chain(
    monkeypatch, policy, want_http_must_be_zero
):
    """#595: the non-recursive batch is admitted per tier per the policy chain,
    so browser-capable jobs are not silently lumped into HTTP admission."""
    admits = await _admission_spy(monkeypatch)

    out = await run_sync_batch(monkeypatch, n_urls=3, policy=policy, total_bytes=1 << 30)

    assert len(out["results"]) == 3, "every submitted page must return a result"
    assert admits, "the sync batch must consult the admission gate"
    if want_http_must_be_zero:
        assert all(want_http == 0 for want_http, _ in admits), (
            f"browser-capable jobs were lumped into HTTP admission: {admits}"
        )
    else:
        assert all(want_browser == 0 for _, want_browser in admits), (
            f"http-only jobs were charged to browser admission: {admits}"
        )
