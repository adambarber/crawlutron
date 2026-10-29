from __future__ import annotations

from enum import StrEnum


class CrawlStatus(StrEnum):
    """How one fetch ended.

    Orthogonal to the taxonomy's ``Classification``, which records whether a host
    was reachable rather than what happened to the bytes.
    """

    success = "success"
    blocked = "blocked"
    disallowed = "disallowed"
    retry_exhausted = "retry_exhausted"
    failed = "failed"
    skipped = "skipped"
