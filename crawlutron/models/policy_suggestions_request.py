"""The request model the policy_suggestions MCP tool validates its arguments against.

The rollup takes no arguments (GET /v1/policy-suggestions is parameterless); the
tool keeps a model anyway so dispatch's boundary type holds -- an empty object
schema refuses every argument the caller invents.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["PolicySuggestionsRequest"]


class PolicySuggestionsRequest(BaseModel):
    """The (empty) argument set of the policy_suggestions tool."""

    model_config = ConfigDict(extra="forbid")
