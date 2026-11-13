"""Single canonical bounded scope matcher.

The "bounded match, fail closed on timeout" wrapper, previously duplicated across the
sitemap filter and BFS loop, lives here once; tests/test_url_doors.py pins the definition.
"""

from __future__ import annotations

import logging

from crawlutron.safety.safe_regex import RegexTimeout, match_bounded
from safe_url.url_redaction import redact_url

__all__ = ["scope_allows"]

_logger = logging.getLogger(__name__)


def _scope_target(url: str) -> str:
    """``scheme://host/path`` without the query: a scope is about hosts and
    paths, so a query-string match must never decide it."""
    return url.split("?", 1)[0]


def scope_allows(compiled, url: str) -> bool:
    """Bounded scope check against ``scheme://host/path`` (query stripped); a
    RegexTimeout fails CLOSED (link excluded), never hangs the crawl or BFS loop.
    """
    try:
        return match_bounded(compiled, _scope_target(url)) is not None
    except RegexTimeout:
        _logger.warning("scope_regex_timeout", extra={"url": redact_url(url)})
        return False
