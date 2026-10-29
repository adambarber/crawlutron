"""The contract a module accepts when it holds state that outlives one call.

The owner publishes the reset, and the protocol is two members -- ``reset_process_state``
plus the testable ``has_process_state`` (a one-member protocol is satisfied by a ``pass`` double).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProcessStateOwner(Protocol):
    """A module holding state that a later caller would otherwise inherit."""

    def reset_process_state(self) -> None:
        """Drop everything this module holds that outlives a single call."""
        ...

    def has_process_state(self) -> bool:
        """Whether anything is currently held.

        Honest in both directions: a module reporting ``False`` while holding a populated
        cache is worse than no probe, because conformance then certifies a reset that failed.
        """
        ...
