"""Response body decompression utilities."""

from __future__ import annotations

import gzip
import io
import zlib
from typing import Protocol, cast


class _BoundedDecompressor(Protocol):
    """The brotlicffi-style API: one call, bounded by an explicit output cap."""

    def decompress(
        self, data: bytes | bytearray | memoryview[int], max_length: int, /
    ) -> bytes: ...


class _StreamingDecompressor(Protocol):
    """The google-brotli API: feed a slice at a time, no cap parameter."""

    def process(self, data: bytes | bytearray | memoryview[int], /) -> bytes: ...


class DecompressionTooLargeError(Exception):
    """Raised when decompressed output exceeds the configured size limit.

    Used as a decompression-bomb guard: a small compressed payload (e.g. a 1 MB
    gzip stream) can expand to gigabytes; callers must bound the output size.
    """


def _passthrough(body: bytes | bytearray | memoryview) -> bytes:
    """Return undecodable input as ``bytes`` without copying when it is already bytes.

    The passthrough sites (gzip/deflate/brotli failure, missing brotli, and the
    no-recognized-encoding tail) all return the caller's buffer; routing them
    through one helper keeps the "do not trust the label" normalization in a
    single place, so a passthrough cannot drift back into handing a caller a
    ``memoryview`` over a buffer the caller may have reused.
    """
    return body if isinstance(body, bytes) else bytes(body)


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
                raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
    return bytes(out)


def decompress_body(
    body: bytes | bytearray | memoryview,
    content_encoding: str,
    *,
    max_output_bytes: int,
) -> bytes:
    """Decompress response body based on Content-Encoding header.

    ``max_output_bytes`` is a required keyword: every caller states the
    decompression-bomb bound explicitly, so a new call site physically cannot
    expand attacker-compressed bytes without a cap (H2 closure — the optional
    default existed only for the uncapped legacy branches, which are gone).

    Exception-safe: if a decoder is unavailable or the body was already
    decompressed (e.g. a transport auto-decompresses and leaves the
    ``Content-Encoding`` header on the response), the original body is
    returned unchanged instead of raising. A genuine over-cap expansion
    raises :class:`DecompressionTooLargeError`.

    ``body`` accepts any readable buffer (callers hold ``bytes``, ``bytearray``
    from incremental assembly, or a ``memoryview`` over a transport buffer).
    The passthrough branches return that same content normalized to ``bytes``:
    a "do not trust the label" fallback for a transport that auto-decompressed
    but left the header. Returning the caller's own buffer type would make the
    return type polymorphic for no caller benefit -- every consumer
    (``HttpFetchResult.body`` is ``bytes``, and
    ``detect_decompression_failure`` compares against the original) reads it as
    bytes -- so the normalization happens at this boundary instead of at each
    passthrough site.
    """
    encoding = content_encoding.lower().strip()

    if "gzip" in encoding:
        try:
            return _decompress_streaming(
                lambda data: gzip.GzipFile(fileobj=io.BytesIO(data)),
                _passthrough(body),
                max_output_bytes,
            )
        except DecompressionTooLargeError:
            raise
        except Exception:
            return _passthrough(body)

    if "deflate" in encoding:
        try:
            out = bytearray()
            d = zlib.decompressobj()
            out.extend(d.decompress(body, max_output_bytes + 1))
            if len(out) > max_output_bytes or d.unconsumed_tail:
                raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
            out.extend(d.flush())
            if len(out) > max_output_bytes:
                raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
            return bytes(out)
        except DecompressionTooLargeError:
            raise
        except Exception:
            return _passthrough(body)

    if "br" in encoding:
        decoded = _decompress_brotli(body, max_output_bytes)
        if decoded is None:
            return _passthrough(body)
        if len(decoded) > max_output_bytes:
            raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
        return bytes(decoded)

    return _passthrough(body)


def _decompress_brotli(
    body: bytes | bytearray | memoryview[int], max_output_bytes: int
) -> bytes | bytearray | None:
    """Decode a brotli body within a cap, or None if this build cannot.

    The brotli ecosystem ships two incompatible streaming APIs: brotlicffi
    exposes ``.decompress(data, cap)`` and google-brotli exposes
    ``.process(data)`` with no cap. Which one is installed is not knowable
    statically, so ``hasattr`` is the discriminator and the cast records what
    that check just proved -- the decoder is typed ``object`` precisely because
    its type depends on the package. Declaring the two shapes as protocols also
    documents them, which ``hasattr`` on an ``object`` did not.

    Returns None when brotli is unavailable or fails, so the caller can decide
    between passthrough and an error rather than this function guessing.
    """
    try:
        import brotli
    except ImportError:
        return None
    try:
        decoder: object | None = brotli.Decompressor()
    except Exception:
        decoder = None

    try:
        if decoder is not None and hasattr(decoder, "decompress"):
            # brotlicffi-style bounded API
            bounded = cast("_BoundedDecompressor", decoder)
            decoded: bytes | bytearray = bounded.decompress(body, max_output_bytes + 1)
        elif decoder is not None and hasattr(decoder, "process"):
            streaming = cast("_StreamingDecompressor", decoder)
            # google-brotli streaming API: feed incrementally and stop past the
            # cap (H6). A single process(body) call would materialize the entire
            # bomb in RAM. Stopping early leaves the output over the cap, which
            # the caller's post-hoc check turns into DecompressionTooLargeError
            # (raising here would be swallowed by the passthrough handler).
            chunks = bytearray()
            step = 16_384
            for offset in range(0, len(body), step):
                chunks.extend(streaming.process(body[offset : offset + step]))
                if len(chunks) > max_output_bytes:
                    break
            decoded = chunks
        else:
            decoded = brotli.decompress(body)
    except Exception:
        return None
    return decoded


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


__all__ = ["DecompressionTooLargeError", "decompress_body", "detect_decompression_failure"]
