"""Hosts and URLs the crawl treats specially, by predicate.

Analytics/social-feed infra is recorded but never enqueued and documents recorded
without download; on the per-link hot path, so membership sets are precomputed here.
"""

from __future__ import annotations

from app.models.normalized_url import NormalizedUrl

# --- Universal non-content hosts (hard-skip) ---
# Tracking/ad/social-feed infrastructure: recorded as links, never enqueued.
ANALYTICS_HOST_SUFFIXES = frozenset(
    {
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googletagservices.com",
        "adservice.google.com",
        "facebook.net",
        "connect.facebook.net",
        "analytics.twitter.com",
        "x.com",
        "pinterest.com",
        "hotjar.com",
        "scorecardresearch.com",
        "mixpanel.com",
        "amplitude.com",
        "segment.io",
        "segment.com",
        "criteo.com",
        "pubmatic.com",
        "rubiconproject.com",
        "adroll.com",
    }
)

# Precomputed forms for the per-link hot paths: dotted suffixes become a tuple
# so ``endswith`` is one C-level call instead of an allocation per suffix.
_ANALYTICS_DOTTED = tuple("." + s for s in ANALYTICS_HOST_SUFFIXES)


def is_analytics_host(url: NormalizedUrl) -> bool:
    """True when ``url`` points at a hard-skip host (apex or any subdomain)."""
    host = url.host
    return host in ANALYTICS_HOST_SUFFIXES or host.endswith(_ANALYTICS_DOTTED)


# --- Non-HTML resource extensions ---
# No further HTML links: discovery records them, EXTRACTION_DOCUMENT fetches.
DOCUMENT_URL_EXTENSIONS = frozenset(
    {".pdf", ".csv", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".rtf"}
)

# Precomputed extension tuple for the is_document_url hot path: one C-level
# ``str.endswith`` pass instead of rebuilding the tuple on every call.
_DOCUMENT_URL_EXTENSIONS = tuple(sorted(DOCUMENT_URL_EXTENSIONS))


def is_document_url(url: NormalizedUrl) -> bool:
    """True when ``url``'s path ends with a known document extension."""
    return url.path.lower().endswith(_DOCUMENT_URL_EXTENSIONS)
