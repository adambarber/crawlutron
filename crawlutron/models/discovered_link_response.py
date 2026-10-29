from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from crawlutron.models.normalized_url import NormalizedUrl


class DiscoveredLinkResponse(BaseModel):
    url: str = Field(description="Raw href value observed on the selected page.")
    normalized_url: NormalizedUrl = Field(
        description="Absolute public HTTP(S) URL resolved against the selected page final URL."
    )
    text: Annotated[str, Field(description="Visible anchor text after whitespace normalization.")] = ""
    rel: list[str] = Field(default_factory=list, description="Anchor rel tokens, when present.")
    source_url: NormalizedUrl = Field(description="Final page URL where this link was discovered.")
    host: str = Field(description="Host for the normalized URL.")
    is_same_host: bool = Field(
        description="True when the discovered link host matches the source page host exactly."
    )
