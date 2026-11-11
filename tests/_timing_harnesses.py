"""Zero-wall instruments, re-exported from the one clock home (#843).

The bodies moved to ``tests/_clocks.py``; the module stays so the twenty
importers keep their path. The name ``clocked`` below is the re-export alias
the invariant sweep reads, not a new definition.
"""

from tests._clocks import (
    HANG_GUARD_SECONDS,
    SleepRecorder,
    await_signalled,
    await_until,
    cpu_seconds,
    recorder,
)

__all__ = [
    "HANG_GUARD_SECONDS",
    "SleepRecorder",
    "await_signalled",
    "await_until",
    "cpu_seconds",
    "recorder",
]
