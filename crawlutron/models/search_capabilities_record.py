"""The search block of the capabilities payload."""

from typing import TypedDict


class SearchCapabilitiesRecord(TypedDict):
    """The ``capabilities["search"]`` block, spelled where the endpoint writes it."""

    active_provider: str
    configured_providers: list[str]
    endpoint: str
    provider_selection: str
