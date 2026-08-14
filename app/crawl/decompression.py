"""Response body decompression utilities."""

import gzip
import zlib


def decompress_body(body: bytes, content_encoding: str) -> bytes:
    """Decompress response body based on Content-Encoding header.

    Exception-safe: if a decoder is unavailable or the body was already
    decompressed (e.g. curl_cffi auto-decompresses a gzip body but leaves the
    ``Content-Encoding: gzip`` header on the response), the original body is
    returned unchanged instead of raising.
    """
    encoding = content_encoding.lower().strip()
    if "gzip" in encoding:
        try:
            return gzip.decompress(body)
        except Exception:
            return body
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except Exception:
            return body
    if "br" in encoding:
        try:
            import brotli
        except ImportError:
            return body
        try:
            return brotli.decompress(body)
        except Exception:
            return body
    return body
