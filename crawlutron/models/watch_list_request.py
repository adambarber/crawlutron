from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class WatchListRequest(BaseModel):
    """Request body for the MCP ``list_watches`` tool; GET /v1/watches takes no query page."""

    model_config = ConfigDict(extra="forbid")
