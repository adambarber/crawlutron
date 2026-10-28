"""The encoding-name sets the decodable-header invariant reads.

Declared support surface (#476): the bounds module and the encoding detector
import these from here, not from each other's internals.
"""

from __future__ import annotations

import re

__all__ = ["_ALWAYS_DECODABLE_ENCODINGS", "_ENCODING_TOKEN", "_KNOWN_ENCODINGS"]

_ALWAYS_DECODABLE_ENCODINGS = {"gzip", "deflate", "identity"}

_ENCODING_TOKEN = re.compile(r"^[a-z0-9\-_+.]+$")

# A comma-joined list is only an encoding list if it MENTIONS a real encoding.
_KNOWN_ENCODINGS = {
    "gzip",
    "x-gzip",
    "deflate",
    "x-deflate",
    "br",
    "zstd",
    "compress",
    "x-compress",
    "identity",
    "*",
}
