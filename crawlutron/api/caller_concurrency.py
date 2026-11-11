"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.caller_concurrency import CallerConcurrencyTracker

__all__ = ["CallerConcurrencyTracker"]
