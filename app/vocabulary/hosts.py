"""Hosts and URLs the crawl treats specially, by predicate.

Analytics and social-feed infrastructure is recorded on pages but never enqueued,
and a document URL is recorded without being downloaded. Both are hot paths --
they run once per discovered link, up to thousands per page -- so the membership
sets are precomputed here and the predicates are the only way to ask.
"""

from __future__ import annotations

from app.models.normalized_url import NormalizedUrl

# --- Universal non-content hosts (hard-skip) ---
# Hosts that never carry crawlable municipal content: tracking/ad/social-feed
# infrastructure. Advertised links to them are still RECORDED on pages, but
# they are never enqueued as crawl jobs (any strategy) and the browser route
# handler aborts their requests unconditionally.
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

# Precomputed forms for the is_analytics_host / is_document_url hot paths
# (both run once per discovered link, up to thousands per page).
# ``is_analytics_host`` is true iff the host *is* a suffix (apex) or ends with
# "." + a suffix (subdomain). The apex-membership test uses the set directly;
# the per-subdomain dotted suffixes are precomputed to a tuple so ``endswith``
# takes a single C-level call instead of allocating ``"." + s`` per suffix.
_ANALYTICS_DOTTED = tuple("." + s for s in ANALYTICS_HOST_SUFFIXES)


def is_analytics_host(url: NormalizedUrl) -> bool:
    """True when ``url`` points at a hard-skip host (apex or any subdomain)."""
    host = url.host
    return host in ANALYTICS_HOST_SUFFIXES or host.endswith(_ANALYTICS_DOTTED)


# --- Non-HTML resource extensions ---
# Documents yield no further HTML links, so URL-discovery crawls record their
# URLs but skip downloading them. Callers requesting EXTRACTION_DOCUMENT opt
# back in to fetching file contents.
DOCUMENT_URL_EXTENSIONS = frozenset(
    {".pdf", ".csv", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".rtf"}
)

# Precomputed extension tuple for the is_document_url hot path: ``str.endswith``
# accepts a tuple and does a single C-level pass instead of re-building the
# tuple from the frozenset on every call.
_DOCUMENT_URL_EXTENSIONS = tuple(sorted(DOCUMENT_URL_EXTENSIONS))


def is_document_url(url: NormalizedUrl) -> bool:
    """True when ``url``'s path ends with a known document extension."""
    return url.path.lower().endswith(_DOCUMENT_URL_EXTENSIONS)
