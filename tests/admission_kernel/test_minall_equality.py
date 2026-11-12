"""L1: MinAll equality is structural, symmetric with the RideCpuWhenFree dataclass.

MinAll is a zero-field scale source; it must equal itself as any frozen dataclass
does and be usable as a stable hashable token."""

from __future__ import annotations

from admission import MinAll


def test_minall_value_equality() -> None:
    """Two MinAll() instances compare equal and hash alike (a frozen dataclass)."""
    # Mutant (plain object identity): MinAll() != MinAll() because each instance
    # has its own id; the frozen dataclass makes them structurally equal.
    assert MinAll() == MinAll()
    assert MinAll() in {MinAll()}
