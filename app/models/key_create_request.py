from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class KeyCreateRequest(BaseModel):
    """The caller-supplied body for issuing a key: a label plus quotas."""

    model_config = ConfigDict(extra="forbid")

    label: Annotated[str, Field(min_length=1, max_length=128)]
    max_pages: Annotated[int, Field(ge=0)] = 0
    max_concurrency: Annotated[int, Field(ge=0)] = 0
