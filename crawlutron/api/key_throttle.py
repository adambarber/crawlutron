"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.key_throttle import KeyVerifyThrottle, shared_throttle

__all__ = ["KeyVerifyThrottle", "shared_throttle"]
