"""Web search capability (``POST /v1/search``)."""

from app.search.providers import (
    BaseProvider,
    SearchProviderError,
    SearchProviderUnavailable,
    active_search_provider_name,
    configured_search_providers,
    get_search_provider,
)

__all__ = [
    "BaseProvider",
    "SearchProviderError",
    "SearchProviderUnavailable",
    "active_search_provider_name",
    "configured_search_providers",
    "get_search_provider",
]
