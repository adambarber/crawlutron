"""The structural door for "a record the serialization walk can render."

The walk's precondition is "a frozen-dataclass instance whose fields are the key
set"; this protocol names that shape so the walk's declared type says the contract.
"""

from __future__ import annotations

from typing import Protocol


class FreezableRecord(Protocol):
    """A dataclass instance renderable by the serialization walk."""
