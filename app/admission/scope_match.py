"""Single canonical bounded scope matcher.

Both the engine's sitemap-seed filter and the recursive BFS loop apply a
caller-supplied scope regex per URL. The identical "bounded match, fail closed
on timeout" wrapper lived in two files, where a future fix (a new caught
exception, an inverted fail direction) applied to one would silently leave the
other on the stale contract. It now lives here once; both import it. ``tests/
test_url_doors.py`` asserts exactly one definition remains.
"""

from __future__ import annotations

import logging

from app.safe_regex import RegexTimeout, match_bounded

logger = logging.getLogger(__name__)


def scope_allows(compiled, url: str) -> bool:
    """Bounded scope check: a RegexTimeout fails CLOSED (link excluded).

    A pathological pattern must never hang the crawl or BFS loop; if the
    wall-clock budget fires, the URL is treated as out-of-scope and the crawl
    continues.
    """
    try:
        return match_bounded(compiled, url) is not None
    except RegexTimeout:
        logger.warning("scope_regex_timeout", extra={"url": url})
        return False
