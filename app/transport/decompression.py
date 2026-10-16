"""Response body decompression utilities."""

from __future__ import annotations

import zlib
from typing import Protocol, cast

import brotli


class _BoundedDecompressor(Protocol):
    """The brotlicffi-style API: one call, bounded by an explicit output cap."""

    def decompress(self, data: bytes | bytearray | memoryview[int], max_length: int, /) -> bytes: ...


class _StreamingDecompressor(Protocol):
    """The google-brotli API: feed a slice at a time, no cap parameter."""

    def process(self, data: bytes | bytearray | memoryview[int], /) -> bytes: ...


# safe: a header naming anything else is passed through whole, because there is no
# ordering that decodes it correctly here.
DECODABLE_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate", "br"})


__all__ = [
    "DECODABLE_ENCODINGS",
    "DecompressionError",
    "DecompressionTooLargeError",
    "decompress_bounded",
    "decompress_body",
]


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


def decompress_bounded(
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


def _decode_one(body: bytes | bytearray | memoryview, coding: str, max_output_bytes: int) -> bytes:
    """Decode one Content-Encoding layer, bounded by the caller's cap -- split
    out so the stacked case loops over one decoder rather than a substring branch
    that returned after the first coding and served the rest raw.
    """
    if coding == "br":
        decoded = _decompress_brotli(body, max_output_bytes)
        if len(decoded) > max_output_bytes:
            raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
        return bytes(decoded)
    # Concatenated members are legal gzip and not legal deflate, which is the
    # only thing the two zlib windings differ by.
    gzip_like = coding in ("gzip", "x-gzip")
    return decompress_bounded(
        lambda: zlib.decompressobj(16 + zlib.MAX_WBITS) if gzip_like else zlib.decompressobj(),
        _passthrough(body),
        max_output_bytes,
        allow_concatenated=gzip_like,
    )


def decompress_body(
    body: bytes | bytearray | memoryview,
    content_encoding: str,
    *,
    max_output_bytes: int,
) -> bytes:
    """Decompress a body by Content-Encoding: ``max_output_bytes`` is a required
    keyword (no call site can expand attacker bytes uncapped) applied to every
    layer; an unreadable header passes the body through unchanged.
    """
    codings = [part.strip() for part in content_encoding.lower().split(",")]
    codings = [coding for coding in codings if coding and coding != "identity"]
    if not codings or not set(codings) <= DECODABLE_ENCODINGS:
        return _passthrough(body)
    decoded: bytes | bytearray | memoryview = body
    for coding in reversed(codings):
        decoded = _decode_one(decoded, coding, max_output_bytes)
    return _passthrough(decoded)


def _decompress_brotli(body: bytes | bytearray | memoryview[int], max_output_bytes: int) -> bytes | bytearray:
    """Decode a brotli body within a cap or raise a typed error. The two brotli
    APIs differ (capped ``decompress`` vs uncapped ``process``); which is installed
    is not statically knowable, so ``hasattr`` discriminates.
    """
    try:
        decoder: object | None = brotli.Decompressor()
    except Exception:
        decoder = None

    try:
        if decoder is not None and hasattr(decoder, "decompress"):
            # Brotlicffi-style bounded API
            bounded = cast(_BoundedDecompressor, decoder)
            decoded: bytes | bytearray = bounded.decompress(body, max_output_bytes + 1)
        elif decoder is not None and hasattr(decoder, "process"):
            streaming = cast(_StreamingDecompressor, decoder)
            # Google-brotli streaming API: feed incrementally and stop past the cap,
            # so the caller's post-hoc check turns over-cap output into the error.
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
