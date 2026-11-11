"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.store_shutdown import close_app_stores

__all__ = ["close_app_stores"]
