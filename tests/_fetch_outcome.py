"""What one fetch produced, typed across the queue; a bare ``None`` reads as a
worker that died, so the reason travels as a type and the success arm is the
production result verbatim, never a copy.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.http_fetch_result import HttpFetchResult


@dataclass(frozen=True, slots=True)
class FetchFailed:
    """The attempt produced nothing; ``error`` names the class that ended it, because
    respecting a transport failure is contract-fine while a hang is not, and the
    caller alone decides what the class name meant.
    """

    error: str
    detail: str = ""


type FetchOutcome = HttpFetchResult | FetchFailed
