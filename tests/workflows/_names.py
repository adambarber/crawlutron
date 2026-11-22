"""Typed workflow primitives. Each rejects what it cannot represent."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

_BOUNDED = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


def _kebab(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value):
        raise ValueError(f"{value!r} must be lowercase kebab-case")
    return value


def _upper_snake(value: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        raise ValueError(f"{value!r} must be UPPER_SNAKE_CASE")
    return value


def _prose(value: str, *, interpolate: bool) -> str:
    if not value.strip() or len(value) > 120 or "\n" in value:
        raise ValueError("name must be 1-120 chars on one line")
    if "${{" in value:
        if not interpolate:
            raise ValueError("a step name must not interpolate an expression")
        if value.count("${{") != value.count("}}"):
            raise ValueError("name has unbalanced expression braces")
    return value.strip()


def _step_name(value: str) -> str:
    return _prose(value, interpolate=False)


def _display_name(value: str) -> str:
    return _prose(value, interpolate=True)


def _action_ref(value: str) -> str:
    if value.startswith(("$/", "./")):
        return value
    if "@" not in value or not re.fullmatch(r"[0-9a-f]{40}", value.rsplit("@", 1)[1]):
        raise ValueError(f"{value!r} is not a 40-hex-pinned action or a local path")
    return value


def _expression(value: str) -> str:
    if value.count("${{") != value.count("}}"):
        raise ValueError("expression has unbalanced braces")
    if "${{" in value and not (value.startswith("${{") and value.endswith("}}")):
        raise ValueError("expression must be fully wrapped or fully bare")
    return value


_MONTHS = frozenset("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())
_DAYS = frozenset("SUN MON TUE WED THU FRI SAT".split())
_CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


def _cron_field(text: str, low: int, high: int, names: frozenset[str]) -> None:
    for part in re.split(r"[,/-]", text):
        if not part or part == "*":
            continue
        if part.isalpha():
            if part.upper() not in names:
                raise ValueError(f"{part!r} is not a cron name")
        elif part.isdigit():
            if not low <= int(part) <= high:
                raise ValueError(f"{part!r} is outside cron range {low}-{high}")
        else:
            raise ValueError(f"{part!r} is not a cron value")


def _cron(value: str) -> str:
    fields = value.split()
    if len(fields) != 5:
        raise ValueError(f"{value!r} is not a five-field cron expression")
    names: tuple[frozenset[str], ...] = (frozenset(), frozenset(), frozenset(), _MONTHS, _DAYS)
    for field, (low, high), allowed in zip(fields, _CRON_RANGES, names, strict=True):
        _cron_field(field, low, high, allowed)
    return value


WorkflowStem = Annotated[_BOUNDED, AfterValidator(_kebab)]
JobId = Annotated[_BOUNDED, AfterValidator(_kebab)]
StepId = Annotated[_BOUNDED, AfterValidator(_kebab)]
EnvName = Annotated[_BOUNDED, AfterValidator(_upper_snake)]
_PROSE = StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
_ONE_LINE = StringConstraints(strip_whitespace=True, min_length=1)
ActionRef = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160), AfterValidator(_action_ref)
]
StepName = Annotated[str, _PROSE, AfterValidator(_step_name)]
DisplayName = Annotated[str, _PROSE, AfterValidator(_display_name)]
Expression = Annotated[str, _ONE_LINE, AfterValidator(_expression)]
Cron = Annotated[str, _ONE_LINE, AfterValidator(_cron)]
TimeoutMinutes = Annotated[int, Field(ge=1, le=120)]
