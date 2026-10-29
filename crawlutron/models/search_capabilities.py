from __future__ import annotations

from pydantic import BaseModel, Field


class SearchCapabilities(BaseModel):
    """Declared web-search capability for ``/v1/capabilities``."""

    active_provider: str = Field(
        description="Provider ``auto`` selection resolves to for the current settings."
    )
    configured_providers: list[str] = Field(
        description="Providers with credentials configured, in priority order."
    )
    endpoint: str = Field(description="The web-search endpoint path.")
    provider_selection: str = Field(description="Human-readable summary of how the provider is selected.")
