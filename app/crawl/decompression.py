"""Response body decompression utilities."""

import gzip
import io
import zlib


class DecompressionTooLargeError(Exception):
    """Raised when decompressed output exceeds the configured size limit.

    Used as a decompression-bomb guard: a small compressed payload (e.g. a 1 MB
    gzip stream) can expand to gigabytes; callers must bound the output size.
    """


def _decompress_streaming(make_fileobj, compressed: bytes, max_output_bytes: int) -> bytes:
    """Decompress ``compressed`` in bounded chunks, raising on overflow."""
    out = bytearray()
    with make_fileobj(compressed) as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > max_output_bytes:
                raise DecompressionTooLargeError(
                    f"decompressed body exceeds {max_output_bytes} bytes"
                )
    return bytes(out)


def decompress_body(body: bytes, content_encoding: str, *, max_output_bytes: int | None = None) -> bytes:
    """Decompress response body based on Content-Encoding header.

    Exception-safe: if a decoder is unavailable or the body was already
    decompressed (e.g. a transport auto-decompresses and leaves the
    ``Content-Encoding`` header on the response), the original body is
    returned unchanged instead of raising.

    When ``max_output_bytes`` is provided, decompression is performed in a
    size-bounded streaming fashion and a :class:`DecompressionTooLargeError`
    is raised if the output would exceed the limit (decompression-bomb guard).
    """
    encoding = content_encoding.lower().strip()

    if "gzip" in encoding:
        if max_output_bytes is None:
            try:
                return gzip.decompress(body)
            except Exception:
                return body
        try:
            return _decompress_streaming(
                lambda data: gzip.GzipFile(fileobj=io.BytesIO(data)), body, max_output_bytes
            )
        except DecompressionTooLargeError:
            raise
        except Exception:
            return body

    if "deflate" in encoding:
        if max_output_bytes is None:
            try:
                return zlib.decompress(body)
            except Exception:
                return body
        try:
            out = bytearray()
            d = zlib.decompressobj()
            out.extend(d.decompress(body, max_output_bytes + 1))
            if len(out) > max_output_bytes or d.unconsumed_tail:
                raise DecompressionTooLargeError(
                    f"decompressed body exceeds {max_output_bytes} bytes"
                )
            out.extend(d.flush())
            if len(out) > max_output_bytes:
                raise DecompressionTooLargeError(
                    f"decompressed body exceeds {max_output_bytes} bytes"
                )
            return bytes(out)
        except DecompressionTooLargeError:
            raise
        except Exception:
            return body

    if "br" in encoding:
        try:
            import brotli
        except ImportError:
            return body
        if max_output_bytes is None:
            try:
                return brotli.decompress(body)
            except Exception:
                return body
        # Bounded brotli decompression. The brotli ecosystem has two
        # incompatible streaming APIs (google-brotli exposes .process(),
        # brotlicffi exposes .decompress(data, cap)) -- detect and use whichever
        # exists. Fall back to full decompress + post-hoc cap check.
        d = None
        try:
            d = brotli.Decompressor()
        except Exception:
            d = None

        try:
            if d is not None and hasattr(d, "decompress"):
                # brotlicffi-style bounded API
                out = d.decompress(body, max_output_bytes + 1)
            elif d is not None and hasattr(d, "process"):
                # google-brotli streaming API
                out = bytes(d.process(body))
            else:
                out = brotli.decompress(body)
        except Exception:
            return body

        if len(out) > max_output_bytes:
            raise DecompressionTooLargeError(
                f"decompressed body exceeds {max_output_bytes} bytes"
            )
        return bytes(out)

    return body


def detect_decompression_failure(
    body: bytes,
    encoding: str,
    decoded: bytes,
) -> bool:
    """Return True when ``decoded`` is just ``body`` passed through unchanged.

    ``decompress_body`` swallows decoder failures and returns the input bytes.
    When a Content-Encoding was advertised but the output equals the raw
    compressed input, the body is garbage and must not be trusted as content.
    """
    if not encoding:
        return False
    codec = encoding.lower().strip()
    if codec in {"identity", "none", ""}:
        return False
    return bool(body) and decoded == body
