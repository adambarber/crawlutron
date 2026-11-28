"""URL redaction for log lines: the ONE chokepoint (#599).

A URL with credentials must never reach a log line; every log/serial form routes
through :func:`redact_url`. Names are decoded ONCE (#659): double-encoded pass.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, quote, unquote, urlsplit

# Query parameters whose values are secrets by name (#659): a DECODED name
# carrying one of these SUBSTRINGS (case-insensitive) is masked regardless of host.
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
_SECRET_NAME_SUBSTRINGS = (
    "secret",
    "token",
    "password",
    "passwd",
    "pwd",
    "apikey",
    "api_key",
    "authorization",
    "jwt",
    "code",
    "hmac",
    "credentials",
)

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
    # #662: compound names hide secrets in their segments -- a name ending with
    # a vocabulary word, or carrying one as a delimited segment, is a secret.
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

    Splits on both ``&`` and ``;``, decodes each name before the secret test,
    and re-encodes the survivors (a decoded value carrying a secret pair too).
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

    Total, never raising: a URL that cannot be split returns fully masked. The
    fragment is dropped entirely: the client never sends it.
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


def _log_safe(text: str) -> str:
    """Escape controls so a redacted value can never forge a log line (#1414)."""
    flat = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return "".join(f"\\x{ord(ch):02x}" if ord(ch) < 32 or ord(ch) == 127 else ch for ch in flat)


def _redact_secret_params_in_text(text: str) -> str:
    """Free text with every secret-named query value masked (#660).

    One linear scan replaces a backtracking regex (#1414): the same decoded-name
    door decides, so prose is never masked.
    """
    pieces: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        marker = text[pos]
        if marker != "?" and marker != "&":
            pieces.append(marker)
            pos += 1
            continue
        name_start = pos + 1
        scan = name_start
        while scan < length and text[scan] != "=" and text[scan] != "&" and not text[scan].isspace():
            scan += 1
        if scan == name_start or scan >= length or text[scan] != "=":
            pieces.append(text[pos:scan])
            pos = scan
            continue
        name = text[name_start:scan]
        value_end = scan + 1
        while value_end < length and text[value_end] != "&" and not text[value_end].isspace():
            value_end += 1
        if is_secret_name(unquote(name)):
            pieces.append(f"{marker}{name}={_REDACTED}")
        else:
            pieces.append(text[pos:value_end])
        pos = value_end
    return "".join(pieces)


def redact_urls_in_text(text: str) -> str:
    """Every URL's log-safe form inside free exception text (#660).

    One linear scan replaces a backtracking regex (#1414); only ``scheme://``
    spans are rewritten, so error prose passes through untouched.
    """
    pieces: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        char = text[pos]
        if not (char.isascii() and char.isalpha()):
            pieces.append(char)
            pos += 1
            continue
        scan = pos + 1
        while scan < length and text[scan].isascii() and (text[scan].isalnum() or text[scan] in "+.-"):
            scan += 1
        if text[scan : scan + 3] != "://" or scan + 3 >= length:
            pieces.append(text[pos:scan])
            pos = scan
            continue
        end = scan + 3
        while end < length and not text[end].isspace():
            end += 1
        pieces.append(redact_url(text[pos:end]))
        pos = end
    return _log_safe(_redact_secret_params_in_text("".join(pieces)))


__all__ = [
    "is_secret_name",
    "redact_url",
    "redact_urls_in_text",
    "redacted_marker",
]
