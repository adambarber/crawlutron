"""The ``GET /v1/policy-suggestions`` body."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from crawlutron.models.host_policy_suggestion import HostPolicySuggestion


class PolicySuggestionsResponse(BaseModel):
    """Candidate host rules drawn from the retained crawl records.

    ``records_examined`` is the window's honest size, and ``records_skipped``
    counts the rows in it that could not be read.
    """

    records_examined: int = Field(description="Retained crawl records the evidence was drawn from.")
    records_skipped: Annotated[int, Field(description="Retained records left out unreadable.")] = 0
    suggestions: list[HostPolicySuggestion] = Field(description="Candidate rules, ordered by host.")
