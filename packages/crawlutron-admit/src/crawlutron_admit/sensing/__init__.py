"""cgroup-aware sensing owned by the admission package.

Core registers this stack's reset pair with its own process-state registry; no
module here imports the crawler.
"""

from crawlutron_admit.sensing.pressure_gates import CpuGate, ResourceGate
from crawlutron_admit.sensing.pressure_memory import MemoryGate, MemoryStats, ResourceGauge, read_memory_stats
from crawlutron_admit.sensing.pressure_probes import LoopLagProbe, read_cpu_quota, read_cpu_usage_seconds

__all__ = [
    "CpuGate",
    "LoopLagProbe",
    "MemoryGate",
    "MemoryStats",
    "ResourceGate",
    "ResourceGauge",
    "read_cpu_quota",
    "read_cpu_usage_seconds",
    "read_memory_stats",
]
