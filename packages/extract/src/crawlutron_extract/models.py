"""Extraction result model: bounded, deterministic output."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["Extraction", "Quality"]


class Quality(StrEnum):
    """Closed vocabulary: high/medium/low/failed."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    FAILED = "failed"


@dataclass(frozen=True)
class Extraction:
    """Bounded deterministic extraction payload with capped lists."""

    text: str = ""
    markdown: str = ""
    charset: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    links: list[dict[str, str]] = field(default_factory=list)
    frames: list[dict[str, str]] = field(default_factory=list)
    forms: list[dict[str, str]] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    quality: Quality = Quality.FAILED
    flags: list[str] = field(default_factory=list)
    truncated: bool = False
