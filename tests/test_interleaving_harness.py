"""The interleaving oracle bites on planted lifecycle breaches, else stays quiet."""

import asyncio
import gc
import warnings

from crawlutron.runtime import process_state
from crawlutron_admit.task_scope import Grace, NamedRegistry, TaskScope
from crawlutron_admit.values import Count, Duration
from tests._support._interleaving import Action, InterleavingOracle, drive

ALL_ACTIONS = frozenset(action.value for action in Action)


def test_driver_is_bounded_and_replays_by_seed():
    first = drive(11, steps=1000)
    again = drive(11, steps=1000)
    assert first.actions == again.actions
    assert len(first.actions) <= 18, "the driver must cap its step budget"
    assert set(first.actions) <= ALL_ACTIONS


def test_every_declared_action_is_reachable_across_seeds():
    seen: set[str] = set()
    for seed in range(12):
        seen.update(drive(seed, steps=18).actions)
    assert seen == ALL_ACTIONS, "an action no seed can choose is dead surface"


def test_a_clean_lifecycle_raises_no_violation():
    result = drive(29, steps=18, reset=process_state.reset_all_process_state)
    assert result.violations == (), result.violations


def test_a_skipped_release_leaves_a_live_socket_the_oracle_names():
    results = [drive(seed, steps=18, reset=lambda: None) for seed in range(6)]
    assert any(any(v.channel == "live_socket" for v in result.violations) for result in results), (
        "a client left open by a skipped reset must be reported"
    )


def test_the_oracle_reports_a_warning():
    with InterleavingOracle() as oracle:
        warnings.warn("lifecycle leaked", ResourceWarning, stacklevel=1)
        found = oracle.observe()
    assert any(v.channel == "warning" and "lifecycle leaked" in v.detail for v in found)


def test_the_oracle_reports_an_unraisable():
    class RaisesOnDel:
        def __del__(self) -> None:
            raise RuntimeError("unraisable lifecycle error")

    with InterleavingOracle() as oracle:
        doomed = RaisesOnDel()
        del doomed
        gc.collect()
        found = oracle.observe()
    assert any(v.channel == "unraisable" for v in found), found


def test_the_oracle_reports_a_pending_task() -> None:
    async def idle() -> None:
        await asyncio.Event().wait()

    async def scenario() -> None:
        registry = NamedRegistry("test:interleaving-pending", Count(1), Duration(1.0))
        scope = TaskScope.process(registry=registry, grace=Grace(1.0))
        handle = scope.spawn_now(idle)
        with InterleavingOracle() as oracle:
            found = oracle.observe(tasks=(handle.task,))
        assert any(v.channel == "pending_task" for v in found), found
        await scope.stop(handle, Grace(1.0))

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.wait_for(scenario(), 2.0))
    finally:
        loop.close()
