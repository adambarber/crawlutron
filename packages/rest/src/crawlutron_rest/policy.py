"""Build the runtime policy snapshot at the composition root.

build_runtime_policy converts CrawlerSettings into the read-only RuntimePolicy
snapshot lower layers import instead of reaching into settings directly.
"""

from __future__ import annotations

from crawlutron.config.policy import RuntimePolicy
from crawlutron.config.settings import CrawlerSettings


def build_runtime_policy(settings: CrawlerSettings) -> RuntimePolicy:
    """Build the read-only runtime policy snapshot from settings.

    The composition root calls this once; lower layers import the snapshot,
    never crawlutron.config.settings.
    """
    return RuntimePolicy(
        search_provider=settings.search_provider,
        tavily_api_key=settings.tavily_api_key,
        brave_api_key=settings.brave_api_key,
        serper_api_key=settings.serper_api_key,
        key_admin_token=settings.key_admin_token,
        search_limit_default=settings.search_limit_default,
        search_limit_max=settings.search_limit_max,
        search_timeout_seconds=settings.search_timeout_seconds,
        respect_robots=settings.respect_robots,
        allowed_hosts=settings.allowed_hosts,
        denied_hosts=settings.denied_hosts,
        default_timeout_seconds=settings.default_timeout_seconds,
        default_max_retries=settings.default_max_retries,
    )
