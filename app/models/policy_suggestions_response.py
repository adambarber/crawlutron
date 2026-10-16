"""The ``GET /v1/policy-suggestions`` body."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.host_policy_suggestion import HostPolicySuggestion


class PolicySuggestionsResponse(BaseModel):
    """Candidate host rules drawn from the retained crawl records.

    ``records_examined`` is the window's honest size, so an empty list is distinct
    from a store that held nothing and evidence is bounded by retention.
    """

    records_examined: int = Field(description="Retained crawl records the evidence was drawn from.")
    suggestions: list[HostPolicySuggestion] = Field(description="Candidate rules, ordered by host.")
