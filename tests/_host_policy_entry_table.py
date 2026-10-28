"""The declared host-policy entry table, as the entry-point gates read it.

Declared support surface (#476): the entry-point gate and its parity test
import this from here, not from each other's internals.
"""

from __future__ import annotations

from app.models.host_policy import HOST_POLICY_ENTRY_POINTS

__all__ = ["host_policy_entry_table"]


def host_policy_entry_table() -> set[str]:
    return set(HOST_POLICY_ENTRY_POINTS)
