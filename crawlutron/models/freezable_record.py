"""The structural door for "a record the serialization walk can render."

The walk's precondition is "a frozen-dataclass instance whose fields are the
key set" (models/serialization.py docstring); this protocol names that shape
so the walk's declared type says the contract, not two lucky members.
"""

from __future__ import annotations

from typing import Protocol


class FreezableRecord(Protocol):
    """A dataclass instance renderable by the serialization walk."""
