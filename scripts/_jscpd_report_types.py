"""The jscpd report's shape, declared once: jscpd's JSON report is an external
boundary whose key set the duplication gate depends on (``firstFile``/``secondFile``/
``fragment``/``tokens``); a dropped key fails at the consumer instead of as ``None``."""

from __future__ import annotations

from typing import NotRequired, TypedDict


class JscpdLocation(TypedDict):
    """One side of a clone: the file jscpd named (root-relative) and its span."""

    name: str
    start: int
    end: int
    tree: NotRequired[str]


class JscpdDuplicate(TypedDict):
    """One clone pair. ``fragment``/``tokens`` are optional: both readers default them."""

    firstFile: JscpdLocation
    secondFile: JscpdLocation
    fragment: NotRequired[str]
    tokens: NotRequired[int]


class JscpdStatistics(TypedDict):
    """The totals block the gate scores. ``lines`` is read with a default."""

    sources: int
    duplicatedLines: int
    lines: NotRequired[int]
    percentage: NotRequired[float]


class JscpdReport(TypedDict):
    """The parsed ``jscpd-report.json``: the totals the gate scores and the pairs."""

    statistics: dict[str, JscpdStatistics]
    duplicates: list[JscpdDuplicate]
