"""URL redaction for log lines: the ONE chokepoint (#599).

A URL with credentials -- userinfo (``user:pass@host``) or query secrets
(``?key=...``) -- must never reach any log line. Objects whose projections can
carry a URL (WebhookTarget, WatchState) route every log/serial form through
:func:`redact_url`; per-site string munging is forbidden.

Accepted limit (#659): the chokepoint decodes each parameter name ONCE.
Double-encoded secrets (``%2573ecret=x``) pass the mask uncaught; a second
decode layer would corrupt benign percent-encoded params, so the leak survives
only where a downstream consumer decodes again.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, urlsplit

# Query parameters whose values are secrets by name (#659): a parameter whose
# DECODED name carries one of these SUBSTRINGS (case-insensitive) is masked
# regardless of host -- substring semantics so the vocabulary cannot age by
# enumeration (client_secret, access_token, id_token, pwd, session all fell
# through the old exact-name list). Exact-name additions are made only where
# a substring would over-redact a legitimate parameter:
# - "session": substring would redact benign names like "session_id" -- but
#   session ids are themselves bearer-adjacent, so it is masked wholesale; the
#   exact entry documents that ruling.
# - "sig"/"signature": "sig" as a substring would over-redact e.g. "design";
#   kept as exact names.
# - "auth": substring would over-redact "author"; kept as an exact name.
# - "username": Credential's basic username is a SecretStr, but "user" as a
#   substring would over-redact user-agent/user-id style parameters; masked as
#   an exact name.
_SECRET_QUERY_KEYS = frozenset(
    {
        "key",
        "token",
        "secret",
        "password",
        "passwd",
        "pass",
        "api_key",
        "apikey",
        "auth",
        "signature",
        "sig",
        "session",
        "username",
        "passphrase",
        "private_key",
        "access_key",
        "api-key",
        "signing_key",
    }
)

#: Substring vocabulary: a decoded parameter name containing any of these
#: (case-insensitive) carries a secret by name.
_SECRET_NAME_SUBSTRINGS = ("secret", "token", "password", "passwd", "pwd", "apikey", "api_key")

_REDACTED = "[REDACTED]"


def redacted_marker() -> str:
    """The one mask literal, as a delegation door for serializers (#660)."""
    return _REDACTED


def _is_secret_name(name: str) -> bool:
    stripped = name.strip().lower().rstrip("[]").rstrip()
    if any(word in stripped for word in _SECRET_NAME_SUBSTRINGS):
        return True
    if stripped in _SECRET_QUERY_KEYS:
        return True
    # #662: compound names hide secrets in their segments -- a name that
    # ends with a vocabulary word or carries one as a delimited segment
    # (private_key, api-key, deploy.token) is a secret by name. Brackets are
    # delimiters too (Rails-style key[s][], pass[word][]).
    segments = [
        s
        for s in stripped.replace(".", "_").replace("-", "_").replace("[", "_").replace("]", "_").split("_")
        if s
    ]
    return bool(segments) and any(s in _SECRET_QUERY_KEYS for s in segments)


def is_secret_name(name: str) -> bool:
    """The public door: whether this decoded query parameter name is secret
    vocabulary (#659). The invariant test reads the same opinion callers run."""
    return _is_secret_name(name)


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


_SECRET_PARAM_SPAN = re.compile(r"[?&]([^=&\s]+)=([^&\s]*)")


def _redact_secret_params_in_text(text: str) -> str:
    """Free text with every secret-named query value masked (#660).

    Raised-into-a-log shapes can carry a query outside any ``scheme://``
    span (a refused ``javascript:`` Location, a bare ``?token=...``); the
    same decoded-name door decides, so prose never gets masked, only
    credential-shaped pairs.
    """
    from urllib.parse import unquote

    def _sub(match: re.Match[str]) -> str:
        name = unquote(match.group(1))
        if is_secret_name(name):
            return f"{match.group(0)[:1]}{match.group(1)}={_REDACTED}"
        return match.group(0)

    return _SECRET_PARAM_SPAN.sub(_sub, text)


def redact_urls_in_text(text: str) -> str:
    """Every URL's log-safe form inside free exception text (#660).

    Raised-into-a-log shapes carry URLs outside any ``url`` extra key; the
    mask runs at the shared sink, not at each raise site. Only
    ``scheme://``-prefixed spans are rewritten, so error prose and
    non-URL detail pass through untouched.
    """
    pieces: list[str] = []
    pos = 0
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", text):
        span = match.group(0)
        pieces.append(text[pos : match.start()])
        pieces.append(redact_url(span))
        pos = match.end()
    pieces.append(text[pos:])
    return _redact_secret_params_in_text("".join(pieces))
