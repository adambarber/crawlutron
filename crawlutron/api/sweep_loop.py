"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.sweep_loop import sweep_forever

__all__ = ["sweep_forever"]
