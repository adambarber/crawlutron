"""The park must never shorten a standing cooldown (#580, cycle 3, attack 4).

An origin's Retry-After is a debt the scheduler holds against itself; the
breaker park stamps its own capped debt on the same field. A stamp that
overwrites lets the breaker's (capped) remaining TRUNCATE a longer 429
window -- the NEXT batch then fires 300s+ early. Debts only grow: the
setter max-combines, and the second batch must wait out what remains.
"""

from tests._clocks import parking_clock
from tests._result_builders import engine_result
from tests._scheduler_doubles import engine_envelope, make_scheduler, trip_circuit
from tests.transport.test_breaker_park import _task


async def test_breaker_park_never_truncates_a_longer_429_window(monkeypatch):
    """429(900) stands; a breaker (400s window) is still open at t=300 when the
    job reaches the breaker check (the pre-semaphore nap burned 300): the park's
    capped stamp must not TRUNCATE the 429 debt -- the second batch may not
    fetch before t=900. Overwrite fires it at t=400 (500s early)."""
    clock, requested = parking_clock(monkeypatch)
    scheduler = make_scheduler(circuit_cooldown_base=400.0, clock=clock)
    scheduler.set_domain_cooldown("example.com", 900.0)
    trip_circuit(scheduler, "example.com")

    ran_at: list[float] = []

    async def worker(job):
        ran_at.append(clock.now)
        return engine_result(job.normalized_url, "rendered")

    first = await scheduler.run([_task(0, "http://example.com/a")], worker, failed_result=engine_envelope)
    # First batch: ONE fetch, inside the window (bounded-batch contract -- the
    # exact nap composition is implementation order, not the contract), naps capped.
    assert first is not None and len(ran_at) == 1 and 0 < ran_at[0] < 900.0, (
        f"the first batch must terminate bounded with one in-window fetch: "
        f"ran_at={ran_at}, sleeps={requested}"
    )
    assert all(d <= 300.0 + 1e-9 for d in requested), f"a nap exceeded the cap: {requested}"

    second = await scheduler.run([_task(0, "http://example.com/a")], worker, failed_result=engine_envelope)
    assert second is not None and len(ran_at) == 2, (
        f"the second batch never fetched: ran_at={ran_at}, sleeps={requested}"
    )
    assert ran_at[1] >= 900.0 - 1e-9, (
        f"the standing 429 debt was truncated: second fetch at t={ran_at[1]} (sleeps: {requested})"
    )
