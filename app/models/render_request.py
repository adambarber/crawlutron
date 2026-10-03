"""Request body for POST /v1/render."""

from __future__ import annotations

from typing import Annotated

from pydantic import ConfigDict, Field

from app.models.render_options_request import RenderOptionsRequest
from app.models.single_url_source_request import SingleUrlSourceRequest


class RenderRequest(SingleUrlSourceRequest):
    """Request body for ``POST /v1/render`` (backward-compatible browser render)."""

    model_config = ConfigDict(extra="forbid")

    # `url`, `urls` and the exactly-one-of validator are inherited, not restated --
    # see the note on CrawlRequest.
    options: Annotated[
        RenderOptionsRequest | None,
        Field(
            description="Browser render options; omitted fields use server defaults.",
        ),
    ] = None
