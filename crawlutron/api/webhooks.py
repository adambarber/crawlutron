"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.webhooks import (
    DELIVERIES_CAP,
    deliver_pinned_webhook,
    ensure_https_webhook_url,
    max_webhook_size,
    post_webhook,
    validate_webhook_url_async,
)

__all__ = [
    "DELIVERIES_CAP",
    "deliver_pinned_webhook",
    "ensure_https_webhook_url",
    "max_webhook_size",
    "post_webhook",
    "validate_webhook_url_async",
]

#: Undeclared service funnels stay importable for historical importers
#: (watch_sweep, async_crawl_runner); the shim mirrors the service surface.
