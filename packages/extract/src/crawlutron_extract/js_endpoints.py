"""JS endpoints: fetch/axios/$.ajax/xhr.open literals admitted via safe_url.

A regex heuristic over script bodies (no JS parser); a URL in a comment is
still admitted; results are deduped and capped.
"""

from __future__ import annotations

import re

from safe_url import NormalizedUrl

from crawlutron_extract._html_common import _prepare, resolve_or_admit
from crawlutron_extract.limits import ExtractLimits

__all__ = ["extract_js_endpoints"]

_FETCH_RE = re.compile(
    r"""(?:fetch|axios\.\w+|\$\.ajax)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_XHR_OPEN_RE = re.compile(
    r"""(?:xmlhttprequest|xhr)\.open\s*\(\s*["'][A-Z]+["'],\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_TEMPLATE_RE = re.compile(
    r"""(?:fetch|axios\.\w+|\$\.ajax|xhr\.open)\s*\(\s*`([^`]+)`""",
    re.IGNORECASE,
)


def extract_js_endpoints(
    html: bytes | str,
    source_url: str | NormalizedUrl | None,
    *,
    limits: ExtractLimits | None = None,
) -> list[NormalizedUrl]:
    """Admitted JS endpoint URLs, in first-seen order, deduped and capped."""
    text, base, limits = _prepare(html, source_url, limits)

    endpoints: list[NormalizedUrl] = []
    seen: set[str] = set()
    for pattern in (_FETCH_RE, _XHR_OPEN_RE, _TEMPLATE_RE):
        for match in pattern.finditer(text):
            literal = match.group(1).strip()
            if not literal:
                continue
            admitted = resolve_or_admit(literal, base)
            if admitted is None or admitted.raw in seen:
                continue
            seen.add(admitted.raw)
            if len(endpoints) >= limits.max_links:
                return endpoints
            endpoints.append(admitted)
    return endpoints
