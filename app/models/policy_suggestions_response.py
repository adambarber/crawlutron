"""The ``GET /v1/policy-suggestions`` body."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.host_policy_suggestion import HostPolicySuggestion


class PolicySuggestionsResponse(BaseModel):
    """Candidate host rules drawn from the retained crawl records.

    ``records_examined`` is the window's honest size, so a caller can tell an
    empty suggestion list from a store that held nothing at all -- and can see
    that the evidence behind every suggestion is bounded by retention rather
    than by the operator's whole crawl history.
    """

    records_examined: int = Field(description="Retained crawl records the evidence was drawn from.")
    suggestions: list[HostPolicySuggestion] = Field(description="Candidate rules, ordered by host.")
