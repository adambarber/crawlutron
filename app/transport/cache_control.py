"""Cache-Control directive parsing for the store and replay seams.

These are the only readers of ``cache-control``/``pragma`` in the transport
layer, so a caller's directive is honored exactly once per decision.
"""

from __future__ import annotations

from collections.abc import Mapping

_NO_STORE = frozenset({"no-store"})
_NO_CACHE = frozenset({"no-cache", "must-revalidate"})


def _directives(headers: Mapping[str, str]) -> set[str]:
    """Lowercased directive tokens from ``cache-control`` plus ``pragma``.

    ``pragma: no-cache`` is the HTTP/1.0 equivalent of the no-cache directive and
    some origins still emit it; both spellings force revalidation, never a store ban.
    """
    merged: list[str] = []
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for header in ("cache-control", "pragma"):
        value = lowered.get(header)
        if value:
            merged.append(value)
    tokens: set[str] = set()
    for chunk in merged:
        for part in chunk.split(","):
            directive = part.split(";", 1)[0].strip().lower()
            tokens.add(directive)
    return tokens


def forbids_store(headers: Mapping[str, str]) -> bool:
    """True when ``no-store`` forbids persisting this response to any cache."""
    return bool(_NO_STORE & _directives(headers))


def requires_revalidation(headers: Mapping[str, str]) -> bool:
    """True when a stored response must be revalidated before it is replayed.

    Covers ``no-cache`` (may store, never serve stale) and ``must-revalidate``,
    plus the HTTP/1.0 ``pragma: no-cache`` spelling.
    """
    return bool(_NO_CACHE & _directives(headers))
