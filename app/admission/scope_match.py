"""Single canonical bounded scope matcher.

The "bounded match, fail closed on timeout" wrapper, previously duplicated across the
sitemap filter and BFS loop, lives here once; tests/test_url_doors.py pins the definition.
"""

from __future__ import annotations

import logging

from app.safety.safe_regex import RegexTimeout, match_bounded

logger = logging.getLogger(__name__)


def scope_allows(compiled, url: str) -> bool:
    """Bounded scope check: a RegexTimeout fails CLOSED (link excluded).

    A pathological pattern must never hang the crawl or BFS loop, so a wall-clock
    budget fire treats the URL as out-of-scope and the crawl continues.
    """
    try:
        return match_bounded(compiled, url) is not None
    except RegexTimeout:
        logger.warning("scope_regex_timeout", extra={"url": url})
        return False
