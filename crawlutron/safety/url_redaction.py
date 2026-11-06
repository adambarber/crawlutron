"""URL redaction for log lines: the ONE chokepoint (#599).

A URL with credentials -- userinfo (``user:pass@host``) or query secrets
(``?key=...``) -- must never reach any log line. Objects whose projections can
carry a URL (WebhookTarget, WatchState) route every log/serial form through
:func:`redact_url`; per-site string munging is forbidden.
"""

from __future__ import annotations

from urllib.parse import urlsplit

# Query parameters whose values are secrets by name; a listed name's value is
# masked regardless of the host.
_SECRET_QUERY_KEYS = frozenset(
    {"key", "token", "secret", "password", "passwd", "pass", "api_key", "apikey", "auth", "signature", "sig"}
)

_REDACTED = "[REDACTED]"


def _redact_query(query: str) -> str:
    """The query string with every secret-named parameter's value masked."""
    pairs: list[str] = []
    for pair in query.split("&"):
        name, sep, value = pair.partition("=")
        if sep and name.lower().strip() in _SECRET_QUERY_KEYS:
            pairs.append(f"{name}={_REDACTED}")
        else:
            pairs.append(pair)
    return "&".join(pairs)


def _masked_netloc(netloc: str) -> str:
    """The netloc with userinfo masked; host and port survive verbatim."""
    userinfo, _, hostport = netloc.rpartition("@")
    if not userinfo:
        return hostport
    return f"{_REDACTED}@{hostport}"


def redact_url(url: str) -> str:
    """The log-safe form of ``url``: userinfo and secret query values masked.

    Total, never raising: a URL that cannot be split returns fully masked --
    an unparseable URL is exactly the shape whose credentials hide from parsing.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if not parts.scheme:
        return "<unparseable-url>"
    masked_query = _redact_query(parts.query)
    netloc = _masked_netloc(parts.netloc)
    fragment = f"#{parts.fragment}" if parts.fragment else ""
    suffix = f"?{masked_query}{fragment}" if masked_query else fragment
    return f"{parts.scheme}://{netloc}{parts.path}{suffix}"
