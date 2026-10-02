from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable error summary.")
    details: dict[str, Any] = Field(default_factory=dict, description="Optional structured error details.")
