"""M3: cancellation state machine -- body cancel releases, pre-lease park cancel releases nothing.

A grant's lease releases via the generator ``finally`` with ``work_completed`` driven by ``sys.exc_info()``;
cancellation in the body frees capacity (no progress), a pre-lease park cancel enters no lease.
"""

from __future__ import annotations

import asyncio

import pytest

from tests._support._doors import Want, _ceiling_one_gate, _followup_grants


async def test_lease_releases_on_cancellation() -> None:
    """Cancelling the body task releases the lease (frees capacity) with work incomplete."""
    gate, want = _ceiling_one_gate()
    grant_time = gate.progress().last_advance.value
    entered = asyncio.Event()
    done = asyncio.Event()

    async def _body_holder() -> None:
        cm_b = gate.admit(want)
        await cm_b.__aenter__()
        entered.set()
        await done.wait()  # hold the lease inside the body

    task = asyncio.create_task(_body_holder())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Mutant: a cancelled body that does not release would leave capacity wedged.
    assert await _followup_grants(gate, want) == 1
    # Mutant: counting the cancelled body as work would advance last_advance.
    assert gate.progress().last_advance.value - grant_time < 4.0


async def test_pre_lease_cancellation_releases_nothing() -> None:
    """Cancelling a parked admit before any grant enters no lease: in-flight is untouched."""
    gate, want = _ceiling_one_gate()
    # Create the parker BEFORE the holder marks this context as in-a-lease, so the
    # parker's copied context sees no lease marker and is not a false nested admit.
    parked = asyncio.create_task(_park(gate, want))
    holder_cm = gate.admit(want)
    holder_grant = await holder_cm.__aenter__()  # ceiling=1, held
    await asyncio.sleep(0.05)
    assert not parked.done()  # genuinely parked for capacity
    parked.cancel()  # cancel the pre-lease park
    with pytest.raises(asyncio.CancelledError):
        await parked
    # The abandoned park entered no lease: releasing the holder lets a NEW admit
    # take exactly one slot (no leaked reservation from the cancelled parker).
    holder_grant.lease.release(work_completed=True)
    await holder_cm.__aexit__(None, None, None)
    assert await _followup_grants(gate, want) == 1


async def _park(gate, want: Want) -> None:
    cm = gate.admit(want)
    g = await cm.__aenter__()  # parks: no ceiling slot available
    g.lease.release(work_completed=True)
    await cm.__aexit__(None, None, None)
