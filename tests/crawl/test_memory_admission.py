"""Memory-adaptive admission control (#self-tuning).

The service senses its cgroup ceiling and current usage, and throttles batch
dispatch so aggregate RSS rides at the configured target (~92%) instead of
OOM-killing at 100%. No operator tuning required.
"""

import pytest

from app.memory_pressure import (
    MemoryGate,
    MemoryStats,
    batch_scale,
    read_memory_stats,
)


# --- sensing ---

def test_read_memory_stats_v2(tmp_path):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "memory.max").write_text("3221225472\n")     # 3 GiB
    (cg / "memory.current").write_text("1610612736\n")  # 1.5 GiB
    stats = read_memory_stats(cgroup_root=str(cg))
    assert stats == MemoryStats(total_bytes=3221225472, used_bytes=1610612736)


def test_read_memory_stats_v2_max_string_falls_back_to_total_ram(tmp_path, monkeypatch):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "memory.max").write_text("max\n")
    (cg / "memory.current").write_text("104857600\n")
    monkeypatch.setattr(
        "app.memory_pressure._total_system_ram", lambda: 8 * 1024 ** 3
    )
    stats = read_memory_stats(cgroup_root=str(cg))
    assert stats.total_bytes == 8 * 1024 ** 3


def test_read_memory_stats_v1(tmp_path):
    cg = tmp_path / "cgroup" / "memory"
    cg.mkdir(parents=True)
    (cg / "memory.limit_in_bytes").write_text("2147483648\n")
    (cg / "memory.usage_in_bytes").write_text("536870912\n")
    stats = read_memory_stats(cgroup_root=str(tmp_path / "cgroup"))
    assert stats.total_bytes == 2147483648
    assert stats.used_bytes == 536870912


def test_read_memory_stats_missing_files_falls_back_to_total_ram(tmp_path, monkeypatch):
    monkeypatch.setattr("app.memory_pressure._total_system_ram", lambda: 4 * 1024 ** 3)
    stats = read_memory_stats(cgroup_root=str(tmp_path / "does-not-exist"))
    assert stats.total_bytes == 4 * 1024 ** 3
    assert stats.used_bytes == 0


# --- governance ---

def test_batch_scale_full_below_target():
    assert batch_scale(used=0.80, total=1.0, target=0.92) == 1.0


def test_batch_scale_scales_linearly_between_target_and_hard():
    # halfway into the soft zone -> roughly half throughput
    s = batch_scale(used=0.945, total=1.0, target=0.92)
    assert 0.0 < s < 1.0
    assert abs(s - 0.5) < 0.2


def test_batch_scale_zero_at_hard_ceiling():
    assert batch_scale(used=0.99, total=1.0, target=0.92) == 0.0


async def test_gate_blocks_dispatch_above_hard_ceiling(monkeypatch):
    """Above the hard ceiling the gate makes callers wait; it releases once
    usage drains back under the target."""
    gate = MemoryGate(target=0.92)
    samples = iter([0.99, 0.98, 0.85])
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("app.memory_pressure.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(gate, "_usage_ratio", lambda: next(samples))

    await gate.wait_while_saturated(max_wait_seconds=10)
    assert len(waits) >= 2          # blocked until the third sample
    assert waits[-1] <= 1.0         # short polling intervals, never long ones


async def test_gate_returns_immediately_under_target():
    gate = MemoryGate(target=0.92)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(gate, "_usage_ratio", lambda: 0.50)
    try:
        await gate.wait_while_saturated(max_wait_seconds=10)  # must not block
    finally:
        monkeypatch.undo()


async def test_engine_throttles_batches_under_memory_pressure(tmp_path, monkeypatch):
    """Integration: with the sampler pinned at 99% usage, the engine pauses
    dispatch (gate waits) yet the crawl still completes."""
    import app.crawl.engine as engine_mod
    from _testkit import site_fetcher

    class PinnedGate(MemoryGate):
        def _usage_ratio(self):
            return 0.99

    gate = PinnedGate(target=0.92)
    gate.max_wait_seconds = 0.05  # keep the suite fast; the pause still happens
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("app.memory_pressure.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(engine_mod, "_memory_gate", gate)

    pages = {
        "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
        "/a": (200, "<html>a</html>", "text/html"),
    }
    out = await engine_mod.crawl_request(
        {"url": "http://example.com/", "policy": {"limits": {"max_depth": 1, "max_pages": 10}}},
        http_fetcher=site_fetcher(pages),
        cache=__import__("app.crawl.cache", fromlist=["HttpCache"]).HttpCache(),
    )
    assert {r["normalized_url"] for r in out["results"]} == {
        "http://example.com",
        "http://example.com/a",
    }
    assert waits, "engine dispatched without consulting the memory gate"
