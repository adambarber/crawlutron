"""An unknowable memory ceiling fails CLOSED, not open (#C11). _total_system_ram returned 0 when sysconf has
no answer (containers without /proc); read_memory_stats fell through to total_bytes=0 and the gate read
0.0 usage -- fully open, forever: a misconfigured container ran ungated.
"""

import pytest

from crawlutron.runtime.pressure_memory import MemoryGate, batch_scale, read_memory_stats


def test_an_unknowable_ceiling_is_conservative_not_zero():
    """No cgroup files + sysconf refusing: the ceiling must be a CONSERVATIVE
    default, never 0 -- the gate must still be able to throttle and pause."""
    stats = read_memory_stats(cgroup_root="/nonexistent", total_ram=0)
    assert stats.total_bytes > 0, f"the ceiling collapsed to 0: the memory gate runs fully open ({stats})"


def test_the_floor_branch_senses_honest_usage_not_zero():
    """The fail-closed floor reports honest usage: nonzero and bounded by the
    ceiling it is measured against, so usage stays an honest fraction of it
    even under parallel load (adversarial pass 5, confirmed defect)."""
    stats = read_memory_stats(cgroup_root="/nonexistent", total_ram=0)
    assert stats.used_bytes > 0, f"the floor branch never sensed usage: {stats}"
    assert stats.used_bytes <= stats.total_bytes, (
        f"usage must never exceed the ceiling it is measured against: {stats}"
    )


def test_the_floor_branch_bounds_usage_by_the_ceiling_under_load():
    """Under load the process's own RSS can ride above the conservative
    ceiling; the floor branch must clamp usage so it never exceeds total."""
    big = bytearray(1_200_000_000)  # push peak RSS above the 1 GiB conservative ceiling
    del big
    stats = read_memory_stats(cgroup_root="/nonexistent", total_ram=0)
    assert 0 < stats.used_bytes <= stats.total_bytes, f"usage exceeds ceiling under load: {stats}"


def test_the_gate_throttles_when_only_a_conservative_ceiling_is_known(monkeypatch):
    """With a conservative ceiling standing in, the gate is a real gate: a usage
    signal at/above the hard ceiling pauses dispatch instead of full speed."""
    gate = MemoryGate(cgroup_root="/nonexistent", usage_ratio=lambda: 0.95)
    assert gate.batch_scale_factor() == 0.0, "a saturated signal must pause dispatch"


def test_batch_scale_never_treats_zero_total_as_full_speed():
    """total=0 carried the fail-open bug into batch_scale itself (1.0 for
    total<=0); the conservative ceiling removes the caller of that branch --
    and batch_scale refuses to report full speed for a zero ceiling."""
    with pytest.raises(ValueError, match="ceiling"):
        batch_scale(0.0, 0.0, 0.85)
