"""Web search capability (``POST /v1/search``)."""

from app.search.providers import (
    SearchProvider,
    SearchProviderError,
    SearchProviderUnavailable,
    active_search_provider_name,
    configured_search_providers,
    get_search_provider,
)

__all__ = [
    "SearchProvider",
    "SearchProviderError",
    "SearchProviderUnavailable",
    "get_search_provider",
    "configured_search_providers",
    "active_search_provider_name",
]
