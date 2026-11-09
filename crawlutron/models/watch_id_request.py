from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WatchIdRequest(BaseModel):
    """The one-watch body both single-watch MCP tools take: ``get_watch`` and ``delete_watch``."""

    model_config = ConfigDict(extra="forbid")

    watch_id: str = Field(..., min_length=1, description="Identifier of the registered watch.")
