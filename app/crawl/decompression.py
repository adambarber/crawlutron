"""Response body decompression utilities."""

import gzip
import zlib
from typing import Optional


def decompress_body(body: bytes, content_encoding: str) -> bytes:
    """Decompress response body based on Content-Encoding header.

    Args:
        body: The response body bytes.
        content_encoding: The Content-Encoding header value.

    Returns:
        Decompressed body bytes, or original body if encoding not supported.
    """
    encoding = content_encoding.lower().strip()
    if "gzip" in encoding:
        return gzip.decompress(body)
    if "deflate" in encoding:
        return zlib.decompress(body)
    if "br" in encoding:
        try:
            import brotli
            return brotli.decompress(body)
        except ImportError:
            pass  # brotli not available, return as-is
    return body
