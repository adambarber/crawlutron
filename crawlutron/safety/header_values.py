"""The one law for headers on the wire: control characters can end one header
and start another, and curl_cffi transmits embedded CRLF verbatim, so every
edge handing a name or value to a transport refuses all C0 controls plus DEL.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

__all__ = [
    "reject_injectable_header_name",
    "reject_injectable_header_value",
    "reject_injectable_header_values",
]

#: The RFC 7230 CTL set: every C0 control plus DEL. Any of them can corrupt
#: HTTP framing in some lenient parser (CR/LF split headers; NUL and the rest
#: poison token parsing); multibyte text stays a legal header value.
_INJECTABLE = re.compile(r"[\x00-\x1f\x7f]")


def reject_injectable_header_name(name: str) -> None:
    """Raise ValueError when the header NAME could smuggle bytes onto the wire."""
    if _INJECTABLE.search(name):
        raise ValueError(
            f"header name '{name}' is not permitted: control bytes can smuggle extra headers onto the wire"
        )


def reject_injectable_header_value(name: str, value: str) -> None:
    """Raise ValueError when ``value`` could smuggle bytes onto the wire; the
    header ``name`` rides the refusal so a caller with many headers can
    bisect the offender.
    """
    if _INJECTABLE.search(value):
        raise ValueError(
            f"header '{name}' value is not permitted: control bytes can smuggle extra headers onto the wire"
        )


def reject_injectable_header_values(headers: Mapping[str, str]) -> None:
    """Apply the name and value laws to a whole header map, at one edge."""
    for name, value in headers.items():
        reject_injectable_header_name(name)
        reject_injectable_header_value(name, value)
