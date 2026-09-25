"""Response body decompression utilities."""

from __future__ import annotations

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


class DecompressionError(ValueError):
    """Raised when a body does not match its declared Content-Encoding."""


class DecompressionTooLargeError(DecompressionError):
    """Raised when decompressed output exceeds the configured size limit.

    Used as a decompression-bomb guard: a small compressed payload (e.g. a 1 MB
    gzip stream) can expand to gigabytes; callers must bound the output size.
    """


def _passthrough(body: bytes | bytearray | memoryview) -> bytes:
    """Normalize unencoded input to bytes without copying an existing bytes value."""
    return body if isinstance(body, bytes) else bytes(body)


def _decompress_bounded(
    make_decompressor,
    compressed: bytes,
    max_output_bytes: int,
    *,
    allow_concatenated: bool,
) -> bytes:
    """Decode every declared member or raise a typed decompression error."""
    try:
        out = bytearray()
        decompressor = make_decompressor()
        pending = compressed
        while pending:
            read_size = min(65536, max_output_bytes - len(out) + 1)
            chunk = decompressor.decompress(pending, read_size)
            pending = decompressor.unconsumed_tail
            out.extend(chunk)
            if len(out) > max_output_bytes:
                raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
            if decompressor.eof:
                pending = decompressor.unused_data
                if pending and not allow_concatenated:
                    raise zlib.error("trailing data after deflate stream")
                if pending:
                    decompressor = make_decompressor()
        if not decompressor.eof:
            raise zlib.error("incomplete compressed stream")
        out.extend(decompressor.flush())
        if len(out) > max_output_bytes:
            raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
        return bytes(out)
    except DecompressionTooLargeError:
        raise
    except Exception as error:
        raise DecompressionError("invalid compressed stream") from error


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

    Malformed, truncated, or unavailable decoder input raises
    :class:`DecompressionError`; a genuine over-cap expansion raises
    :class:`DecompressionTooLargeError`. Unknown encodings pass through unchanged.

    ``body`` accepts bytes-like input; the unknown-encoding passthrough normalizes
    it to ``bytes`` at this boundary because all consumers read the result as bytes.
    """
    encoding = content_encoding.lower().strip()

    if "gzip" in encoding or "deflate" in encoding:
        return _decompress_bounded(
            lambda: zlib.decompressobj(16 + zlib.MAX_WBITS)
            if "gzip" in encoding
            else zlib.decompressobj(),
            _passthrough(body),
            max_output_bytes,
            allow_concatenated="gzip" in encoding,
        )

    if "br" in encoding:
        decoded = _decompress_brotli(body, max_output_bytes)
        if len(decoded) > max_output_bytes:
            raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
        return bytes(decoded)

    return _passthrough(body)


def _decompress_brotli(
    body: bytes | bytearray | memoryview[int], max_output_bytes: int
) -> bytes | bytearray:
    """Decode a brotli body within a cap or raise a typed error.

    The two brotli APIs are incompatible: brotlicffi exposes
    ``.decompress(data, cap)`` and google-brotli ``.process(data)`` with no cap.
    Which is installed is not knowable statically, so ``hasattr`` is the
    discriminator and the cast records what that check just proved -- the decoder
    is typed ``object`` for exactly that reason. The Protocols document the two
    shapes, which ``hasattr`` on an ``object`` did not.
    """
    try:
        import brotli
    except ImportError as error:
        raise DecompressionError("brotli decoder is unavailable") from error
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
            # the caller's post-hoc check turns into DecompressionTooLargeError.
            chunks = bytearray()
            step = 16_384
            for offset in range(0, len(body), step):
                chunks.extend(streaming.process(body[offset : offset + step]))
                if len(chunks) > max_output_bytes:
                    break
            decoded = chunks
        else:
            decoded = brotli.decompress(cast(bytes, body))
    except Exception as error:
        raise DecompressionError("invalid brotli stream") from error
    return decoded


__all__ = ["DecompressionError", "DecompressionTooLargeError", "decompress_body"]
