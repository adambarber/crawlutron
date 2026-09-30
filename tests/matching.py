"""Composable predicates with self-describing failures — this project's RSpec custom matcher.

A test that asserts a property wants two things from the thing it asserts with:
a decision, and an explanation of the decision good enough that the reader does
not have to open the assertion to learn what was expected. Hand-written
``assert_*`` helpers give the first and usually improvise the second, and they
cannot be combined. ``be_empty() and not_contains(x) and len(y) <= 3`` is three
helpers that each raise their own message; here it is one expression that
explains all three and names the one that failed.

Three properties this buys over a pile of ``assert_*`` functions:

* **Composable.** ``__and__``, ``__or__`` and ``__invert__`` build new matchers,
  so a rule that needs "calls the owner *and* never the legacy spelling" is
  written as one expression instead of two helpers that each re-list the
  offenders.
* **Self-describing.** ``describe`` returns prose about the actual value. A
  detector that fires says which file, which line and what the rule wanted, and
  the same string is the assertion message and the pytest failure text, so there
  is no second copy of the wording to drift.
* **Verifiable on its own.** ``verify`` raises ``AssertionError``, so a matcher is
  testable without pytest, which is what lets a rule's own bite proof be an
  assertion about the matcher rather than a second hand-written test.

The negative form is the whole trick, and it is why ``Not`` is a real class
rather than a ``lambda``. ``Not(x).describe(actual)`` has to say *why x rejected
it*, which means the inner matcher must be asked for its reason and have it
prefixed, not merely have its boolean flipped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


class Matcher:
    """A predicate that can also explain itself.

    Subclasses implement :meth:`matches` and :meth:`describe`. ``verify`` is
    final: it is the only place an assertion is raised, so every matcher in the
    suite fails the same way.
    """

    def matches(self, actual: Any) -> bool:
        raise NotImplementedError

    def describe(self, actual: Any) -> str:
        """Prose describing what this matcher wanted of ``actual``."""
        raise NotImplementedError

    def verify(self, actual: Any, *, reason: str | None = None) -> None:
        """Raise ``AssertionError`` unless ``actual`` satisfies the matcher.

        ``reason`` is prepended to the matcher's own description. It exists for
        the case where the matcher alone cannot name the thing under test: a
        detector that runs over every file in ``app/`` knows what it rejected,
        but not which loop iteration was looking at it.
        """
        if self.matches(actual):
            return
        detail = self.describe(actual)
        raise AssertionError(f"{reason}: {detail}" if reason else detail)

    def __and__(self, other: Matcher) -> AllOf:
        return AllOf((self, other))

    def __or__(self, other: Matcher) -> AnyOf:
        return AnyOf((self, other))

    def __invert__(self) -> Not:
        return Not(self)

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"


@dataclass(frozen=True)
class AllOf(Matcher):
    """Every part must match. Reports only the parts that actually failed.

    Joining all reasons is what makes composition useful: a three-part rule
    failing on one part should not print the noise of the two that passed.
    """

    parts: tuple[Matcher, ...]

    def matches(self, actual: Any) -> bool:
        return all(part.matches(actual) for part in self.parts)

    def describe(self, actual: Any) -> str:
        failed = [part.describe(actual) for part in self.parts if not part.matches(actual)]
        return " and ".join(failed)

    def __repr__(self) -> str:
        return f"all_of({', '.join(repr(p) for p in self.parts)})"


@dataclass(frozen=True)
class AnyOf(Matcher):
    """At least one part must match. Reports every reason when none do."""

    parts: tuple[Matcher, ...]

    def matches(self, actual: Any) -> bool:
        return any(part.matches(actual) for part in self.parts)

    def describe(self, actual: Any) -> str:
        return " or ".join(part.describe(actual) for part in self.parts)

    def __repr__(self) -> str:
        return f"any_of({', '.join(repr(p) for p in self.parts)})"


@dataclass(frozen=True)
class Not(Matcher):
    """Inverts a matcher, keeping the inner reason readable.

    The description is ``<inner reason> (negated)`` rather than the bare inner
    reason: ``not_contains(x)`` failing on a value that *does* contain ``x``
    must not read as though the matcher wanted the value to contain it.
    """

    part: Matcher

    def matches(self, actual: Any) -> bool:
        return not self.part.matches(actual)

    def describe(self, actual: Any) -> str:
        return f"NOT({self.part.describe(actual)})"

    def __repr__(self) -> str:
        return f"not_({self.part!r})"


@dataclass(frozen=True)
class _Describing:
    """Lifts a bare predicate into a matcher, for the cases that need no class.

    Most detector fragments are one line of ``lambda``. Giving each a named class
    would be more code than the check is worth, so this supplies the
    self-describing half and the caller supplies the wording. The predicate is
    stored under ``repr`` so a failing composite names which half rejected the
    value.
    """

    label: str
    predicate: Callable[[Any], bool]

    def matches(self, actual: Any) -> bool:
        return bool(self.predicate(actual))

    def describe(self, actual: Any) -> str:
        return f"{self.label} (actual: {actual!r})"

    def __repr__(self) -> str:
        return self.label


def describing(label: str, predicate: Callable[[Any], bool]) -> Matcher:
    """A named matcher over a one-line predicate."""
    return _Describing(label, predicate)


def all_of(*parts: Matcher) -> Matcher:
    """Every matcher must match. A single part is returned unchanged."""
    flattened = tuple(p for part in parts for p in (part.parts if isinstance(part, AllOf) else (part,)))
    return flattened[0] if len(flattened) == 1 else AllOf(flattened)


def any_of(*parts: Matcher) -> Matcher:
    """At least one matcher must match. A single part is returned unchanged."""
    return parts[0] if len(parts) == 1 else AnyOf(tuple(parts))


def not_(part: Matcher) -> Matcher:
    """Inverts ``part``."""
    return ~part


def join(details: Iterable[str], *, empty: str) -> str:
    """Join offender strings, or return ``empty`` when there are none."""
    listed = "; ".join(details)
    return listed or empty
