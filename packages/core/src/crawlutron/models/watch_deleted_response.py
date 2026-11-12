from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WatchDeletedResponse(BaseModel):
    """The MCP delete outcome as a typed body (the REST DELETE door answers 204)."""

    model_config = ConfigDict(extra="ignore")

    deleted: bool = Field(description="Whether the watch row was removed.")
    watch_id: str = Field(description="Identifier of the deleted watch.")
