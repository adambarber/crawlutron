"""Type definitions for review probes."""

from __future__ import annotations

from dataclasses import dataclass

from crawlutron.review.support import ProbeResultRecord


@dataclass(frozen=True, slots=True)
class ProbeArtifact:
    """One structured probe artifact."""

    name: str
    result: ProbeResultRecord
    data: dict[str, object]
