from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, Field

from app.models.json_value import JsonValue


class ErrorEnvelope(TypedDict):
    """The wire error shape: a stable code, a message, and open details.

    ``details`` is carried verbatim to the serialized body, so it is
    ``JsonValue`` -- arbitrary JSON -- not a declared record.
    """

    code: str
    message: str
    details: JsonValue


class ErrorResponse(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable error summary.")
    details: dict[str, Any] = Field(default_factory=dict, description="Optional structured error details.")
