from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KeyCreateRequest(BaseModel):
    """The caller-supplied body for issuing a key: a label plus quotas."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=128)]

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise ValueError("key label must be 1..128 characters")
        return value

    max_pages: Annotated[int, Field(ge=0)] = 0
    max_concurrency: Annotated[int, Field(ge=0)] = 0
