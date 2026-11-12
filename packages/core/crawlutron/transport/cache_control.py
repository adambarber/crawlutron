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


def _directive_argument(headers: Mapping[str, str], name: str) -> float | None:
    """The numeric argument of the first ``name`` directive, or None."""
    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    for part in lowered.get("cache-control", "").split(","):
        directive, _, argument = part.partition("=")
        if directive.strip().lower() == name:
            try:
                return float(argument.strip().strip('"'))
            except ValueError:
                continue
    return None


def forbids_store(headers: Mapping[str, str]) -> bool:
    """True when ``no-store`` forbids persisting this response to any cache."""
    return bool(_NO_STORE & _directives(headers))


def forbids_shared_store(headers: Mapping[str, str]) -> bool:
    """RFC 9111 5.2.2.6: ``private`` marks single-user content -- a shared cache
    stores nothing, so it can never be replayed to another caller. ``no-store``
    already bans every cache, shared included."""
    return forbids_store(headers) or "private" in _directives(headers)


def max_age_seconds(headers: Mapping[str, str]) -> float | None:
    """The shared-cache freshness lifetime (RFC 9111): s-maxage (5.2.2.9) wins
    over max-age (5.2.2.1); None when neither parses."""
    for name in ("s-maxage", "max-age"):
        seconds = _directive_argument(headers, name)
        if seconds is not None:
            return seconds
    return None


def expires_seconds(headers: Mapping[str, str], *, now: float) -> float | None:
    """The freshness lifetime implied by the ``Expires`` instant (RFC 9111 5.3):
    the header date minus ``now``; None when absent or unparsable."""
    from email.utils import parsedate_to_datetime

    lowered = {str(k).lower(): str(v) for k, v in headers.items()}
    raw = lowered.get("expires")
    if not raw:
        return None
    try:
        instant = parsedate_to_datetime(raw).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return instant - now


def requires_revalidation(headers: Mapping[str, str]) -> bool:
    """True when a stored response must be revalidated before it is replayed.

    Covers ``no-cache`` (may store, never serve stale) and ``must-revalidate``,
    plus the HTTP/1.0 ``pragma: no-cache`` spelling.
    """
    return bool(_NO_CACHE & _directives(headers))
