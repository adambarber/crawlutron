"""The request model the policy_suggestions MCP tool validates against.

The rollup takes no arguments; the tool keeps a model so dispatch's boundary
type holds -- an empty object schema refuses every argument the caller invents.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["PolicySuggestionsRequest"]


class PolicySuggestionsRequest(BaseModel):
    """The (empty) argument set of the policy_suggestions tool."""

    model_config = ConfigDict(extra="forbid")
