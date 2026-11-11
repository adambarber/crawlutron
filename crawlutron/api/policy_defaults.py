"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.policy_defaults import (
    BROWSER_FALLBACK_POLICY,
    DOCUMENT_EXTRACTION_POLICY,
)

__all__ = ["BROWSER_FALLBACK_POLICY", "DOCUMENT_EXTRACTION_POLICY"]
