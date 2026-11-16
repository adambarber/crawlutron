"""The spawned cffi child's hang guard is derived from a measured cost.

A fixed ``join_timeout`` was a magic number against a load-sensitive cost
(#1109): the bound is the measured floor plus the caller's timeout, times the factor."""

from multiprocessing.process import BaseProcess

import pytest

from tests._fetcher_factories import real_cffi_fetch_outcome

# The guard's stated factor, pinned beside the derivation it guards: a silent
# edit of the factor reds here, naming the change.
_GUARD_FACTOR = 8.0


@pytest.mark.real_cffi
@pytest.mark.parametrize(("timeout_seconds", "measured_floor"), [(1.0, 1.0), (2.5, 0.5)])
def test_the_hang_guard_is_the_measured_floor_plus_timeout_times_the_factor(
    monkeypatch, timeout_seconds: float, measured_floor: float
) -> None:
    """The join bound moves with the measured floor and the caller's timeout.

    Stub the floor, record what ``join_or_kill`` is handed: the child proves the
    fetch door answers, the assertion is the bound -- a fixed bound cannot scale."""
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "1")
    recorded: list[float] = []

    def _stub_join_or_kill(worker: BaseProcess, join_timeout: float, what: str) -> None:
        recorded.append(join_timeout)
        worker.join()

    monkeypatch.setattr(
        "tests._transport_doubles.join_or_kill",
        _stub_join_or_kill,
    )
    monkeypatch.setattr(
        "tests._transport_doubles._measured_child_floor_seconds",
        lambda floor=measured_floor: floor,
    )

    real_cffi_fetch_outcome("http://127.0.0.1:1/", timeout_seconds=timeout_seconds)

    assert recorded == [(measured_floor + timeout_seconds) * _GUARD_FACTOR]
