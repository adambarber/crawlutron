"""The wire projection of the owner's metadata record."""

from __future__ import annotations

from pydantic import BaseModel


class PageMetadataResponse(BaseModel):
    """The wire projection of :class:`~app.models.page_metadata.PageMetadata`."""

    title: str | None = None
    description: str | None = None
    language: str | None = None
    og_title: str | None = None
    og_description: str | None = None
    og_image: str | None = None
    og_url: str | None = None
    og_type: str | None = None
    og_site_name: str | None = None
    twitter_card: str | None = None
    twitter_title: str | None = None
    twitter_description: str | None = None
    twitter_image: str | None = None
    canonical_url: str | None = None
    favicon: str | None = None
    source_url: str | None = None
    json_ld: dict | list | None = None
