"""The one law for headers on the wire: CR, LF, and NUL can end
one header and start another, and curl_cffi transmits embedded CRLF verbatim,
so every edge handing a name or value to a transport refuses these bytes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

__all__ = [
    "reject_injectable_header_name",
    "reject_injectable_header_value",
    "reject_injectable_header_values",
]

#: CR and LF are the wire delimiters; NUL poisons lenient parsers. Nothing else
#: is refused -- multibyte text is a legal header value.
_INJECTABLE = re.compile(r"[\r\n\x00]")


def reject_injectable_header_name(name: str) -> None:
    """Raise ValueError when the header NAME could smuggle bytes onto the wire."""
    if _INJECTABLE.search(name):
        raise ValueError(
            f"header name '{name}' is not permitted: CR, LF, and NUL bytes can "
            "smuggle extra headers onto the wire"
        )


def reject_injectable_header_value(name: str, value: str) -> None:
    """Raise ValueError when ``value`` could smuggle bytes onto the wire; the
    header ``name`` rides the refusal so a caller with many headers can
    bisect the offender.
    """
    if _INJECTABLE.search(value):
        raise ValueError(
            f"header '{name}' value is not permitted: CR, LF, and NUL bytes can "
            "smuggle extra headers onto the wire"
        )


def reject_injectable_header_values(headers: Mapping[str, str]) -> None:
    """Apply the name and value laws to a whole header map, at one edge."""
    for name, value in headers.items():
        reject_injectable_header_name(name)
        reject_injectable_header_value(name, value)
