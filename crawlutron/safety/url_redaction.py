"""URL redaction for log lines: the ONE chokepoint (#599).

A URL with credentials -- userinfo (``user:pass@host``) or query secrets
(``?key=...``) -- must never reach any log line. Objects whose projections can
carry a URL (WebhookTarget, WatchState) route every log/serial form through
:func:`redact_url`; per-site string munging is forbidden.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, urlsplit

# Query parameters whose values are secrets by name; a listed name's value is
# masked regardless of the host. Membership runs on the DECODED name: a
# percent-encoded or bracketed spelling is the same parameter on the wire.
_SECRET_QUERY_KEYS = frozenset(
    {"key", "token", "secret", "password", "passwd", "pass", "api_key", "apikey", "auth", "signature", "sig"}
)

_REDACTED = "[REDACTED]"


def _is_secret_name(name: str) -> bool:
    stripped = name.strip().lower()
    return stripped in _SECRET_QUERY_KEYS or stripped.rstrip("[]").rstrip() in _SECRET_QUERY_KEYS


def _redact_query(query: str) -> str:
    """The query string with every secret-named parameter's value masked.

    Splits on both ``&`` and ``;`` (the historical browser separator), decodes
    each name before the secret test, and re-encodes the surviving pairs. A
    decoded value that itself carries a secret-named pair is masked too: a
    percent-encoded ``;`` is a pair separator to some parsers.
    """
    pairs: list[str] = []
    for chunk in query.replace(";", "&").split("&"):
        for raw_name, value in parse_qsl(chunk, keep_blank_values=True):
            if _is_secret_name(raw_name):
                pairs.append(f"{raw_name}={_REDACTED}")
            else:
                # Re-encode: decoding must never resurrect separators or
                # delimiters that canonicalization encoded away.
                if ";" in value or "&" in value:
                    encoded_value = _redact_query(value)
                else:
                    encoded_value = quote(value, safe="")
                pairs.append(f"{quote(raw_name, safe='')}={encoded_value}")
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
    an unparseable URL is exactly the shape whose credentials hide from
    parsing. The fragment is dropped entirely: the client never sends it, so
    anything there is secret-carried.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable-url>"
    if not parts.scheme:
        return "<unparseable-url>"
    masked_query = _redact_query(parts.query)
    netloc = _masked_netloc(parts.netloc)
    suffix = f"?{masked_query}" if masked_query else ""
    return f"{parts.scheme}://{netloc}{parts.path}{suffix}"
