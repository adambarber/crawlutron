"""Browser render options for /v1/render (all optional; server applies defaults)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.vocabulary.limits import MAX_HTTP_TIMEOUT_SECONDS, MAX_RETRIES


class RenderOptionsRequest(BaseModel):
    """Browser render options for ``/v1/render`` (all optional; server applies defaults)."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: Annotated[
        int | None,
        Field(
            description=(
                "Per-render navigation timeout in seconds; values above "
                f"{MAX_HTTP_TIMEOUT_SECONDS} are clamped to it."
            ),
        ),
    ] = None
    max_retries: Annotated[
        int | None,
        Field(
            description=(
                "Maximum retries for a transient render failure; values above "
                f"{MAX_RETRIES} are clamped to it."
            ),
        ),
    ] = None
    respect_robots: Annotated[
        bool | None,
        Field(
            description="Whether to honor robots.txt for the render target.",
        ),
    ] = None
    include_html: Annotated[bool | None, Field(description="Include the rendered HTML in the result.")] = None
    include_text: Annotated[
        bool | None,
        Field(
            description="Include extracted visible text in the result.",
        ),
    ] = None
