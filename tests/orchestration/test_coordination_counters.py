"""Cross-process shared counters: the substrate every multi-worker fact needs.

Plain increments and a thread lock are invisible across processes, so with N
workers ``/metrics`` served one worker's numbers, not a shared total.
"""

import pytest

from app.storage.coordination_counters import CoordinationCounters


def test_increments_from_independent_connections_sum(tmp_path):
    db = tmp_path / "coordination.db"
    first = CoordinationCounters(db)
    second = CoordinationCounters(db)

    first.add("crawl_total", 2)
    second.add("crawl_total", 3)

    # Each object must see the OTHER's write. A per-process store cannot.
    assert first.get("crawl_total") == 5
    assert second.get("crawl_total") == 5


def test_negative_deltas_decrement_and_missing_names_read_zero(tmp_path):
    counters = CoordinationCounters(tmp_path / "coordination.db")

    counters.add("crawl_total", 5)
    counters.add("crawl_total", -2)
    assert counters.get("crawl_total") == 3
    # An unseen name is zero, not None: /metrics must render a full series rather than omit names that only
    # some workers have touched.
    assert counters.get("never_recorded") == 0


def test_snapshot_returns_every_recorded_name(tmp_path):
    """The endpoint sums across workers, so it must be able to enumerate them."""
    counters = CoordinationCounters(tmp_path / "coordination.db")

    counters.add("crawl_total", 1)
    counters.add("render_total", 2)

    assert counters.snapshot() == {"crawl_total": 1, "render_total": 2}


def test_reset_clears_every_recorded_name(tmp_path):
    """Test isolation depends on a counter store being resettable in place."""
    counters = CoordinationCounters(tmp_path / "coordination.db")
    counters.add("crawl_total", 4)

    counters.reset()

    assert counters.snapshot() == {}


def test_name_is_bound_as_a_parameter_never_interpolated(tmp_path):
    """Counter names come from metric labels; SQL has no place for that.

    A name is a bound parameter in every write and read, so a name carrying SQL
    is stored as the literal text it is rather than executed.
    """
    counters = CoordinationCounters(tmp_path / "coordination.db")
    hostile = "x'); DROP TABLE coordination_counters; --"

    counters.add(hostile, 7)

    assert counters.get(hostile) == 7
    # The table survived, so the name was data and never code.
    assert counters.snapshot() == {hostile: 7}


def test_counters_survive_a_reopen(tmp_path):
    """A worker restart must not silently zero the service's telemetry."""
    db = tmp_path / "coordination.db"

    CoordinationCounters(db).add("crawl_total", 9)

    assert CoordinationCounters(db).get("crawl_total") == 9


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_deltas_are_rejected(tmp_path, value):
    """A NaN delta poisons the counter permanently -- no later value recovers it.

    SQLite happily stores NaN as NULL, which turns the counter into a missing
    row and makes the whole series read as zero. Reject at the chokepoint.
    """
    counters = CoordinationCounters(tmp_path / "coordination.db")

    with pytest.raises(ValueError):
        counters.add("crawl_total", value)

    assert counters.get("crawl_total") == 0
