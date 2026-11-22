"""One public construction protocol for every validated workflow model."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol, Self, runtime_checkable


@runtime_checkable
class Admitted(Protocol):
    """Every model is built through the same two names, never through a dialect.

    ``create`` is the sole admission door and raises ``ValueError`` with a named
    refusal literal. ``admit_or_none`` is the one None-returning wrapper.
    """

    @classmethod
    def create(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Self: ...

    @classmethod
    def admit_or_none(cls, raw: object, *, context: Mapping[str, object] | None = None) -> Self | None: ...


def admit_or_none[T](factory: Callable[[object], T], raw: object) -> T | None:
    """Refusal literals map to None; foreign errors propagate."""
    try:
        return factory(raw)
    except ValueError:
        return None
