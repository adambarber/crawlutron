"""The ``GET /v1/watches`` listing."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.watch_state import WatchState


class WatchListResponse(BaseModel):
    """Every registered watch, each with the state of its last check."""

    model_config = ConfigDict(extra="ignore")

    watches: list[WatchState]
