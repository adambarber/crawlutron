"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.admit_endpoint import admit_url, known_reasons

__all__ = ["admit_url", "known_reasons"]
