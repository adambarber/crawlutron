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


# The codings this process can undo. Membership is what makes the stacked path
# safe: a header naming anything else is passed through whole, because there is
# no ordering that decodes it correctly here.
_DECODABLE_ENCODINGS = frozenset({"gzip", "x-gzip", "deflate", "br"})


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


def _decode_one(body: bytes | bytearray | memoryview, coding: str, max_output_bytes: int) -> bytes:
    """Decode one Content-Encoding layer, bounded by the caller's cap.

    Split out so the stacked case is a loop over the same single-layer decoder
    rather than a second selection: a header naming several codings used to pick
    one branch on a substring test and return, so ``gzip, br`` lost only its gzip
    layer and served the brotli bytes as if they were the body.
    """
    if coding == "br":
        decoded = _decompress_brotli(body, max_output_bytes)
        if len(decoded) > max_output_bytes:
            raise DecompressionTooLargeError(f"decompressed body exceeds {max_output_bytes} bytes")
        return bytes(decoded)
    # Concatenated members are legal gzip and not legal deflate, which is the
    # only thing the two zlib windings differ by.
    gzip_like = coding in ("gzip", "x-gzip")
    return _decompress_bounded(
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
    """Decompress response body based on Content-Encoding header.

    ``max_output_bytes`` is a required keyword: every caller states the
    decompression-bomb bound explicitly, so a new call site physically cannot
    expand attacker-compressed bytes without a cap (the optional
    default existed only for the uncapped legacy branches, which are gone).
    Every layer is held to it, not just the outermost, so an intermediate stage
    of a stack cannot expand past the bound on its way to a small final body.

    Codings are listed in the order they were applied, so they are undone in
    reverse. ``identity`` names the absence of a coding and is dropped.

    Malformed, truncated, or unavailable decoder input raises
    :class:`DecompressionError`; a genuine over-cap expansion raises
    :class:`DecompressionTooLargeError`. A header this process cannot read end to
    end — an unknown coding, alone or stacked — passes the body through unchanged
    rather than decoding the part it recognises: raw bytes fail downstream as a
    parse error the caller can see, where half-decoded bytes would be a
    successful fetch of content the server never sent.

    ``body`` accepts bytes-like input; the passthrough normalizes it to ``bytes``
    at this boundary because all consumers read the result as bytes.
    """
    codings = [part.strip() for part in content_encoding.lower().split(",")]
    codings = [coding for coding in codings if coding and coding != "identity"]
    if not codings or not set(codings) <= _DECODABLE_ENCODINGS:
        return _passthrough(body)
    decoded: bytes | bytearray | memoryview = body
    for coding in reversed(codings):
        decoded = _decode_one(decoded, coding, max_output_bytes)
    return _passthrough(decoded)


def _decompress_brotli(body: bytes | bytearray | memoryview[int], max_output_bytes: int) -> bytes | bytearray:
    """Decode a brotli body within a cap or raise a typed error.

    The two brotli APIs are incompatible: brotlicffi exposes
    ``.decompress(data, cap)`` and google-brotli ``.process(data)`` with no cap.
    Which is installed is not knowable statically, so ``hasattr`` is the
    discriminator and the cast records what that check just proved -- the decoder
    is typed ``object`` for exactly that reason. The Protocols document the two
    shapes, which ``hasattr`` on an ``object`` did not.

    That ambiguity is about WHICH brotli is installed, not whether one is. The
    module is a declared dependency, so there is no availability to probe: the
    old ``try: import brotli`` here and the ``_brotli_available`` it fed existed
    for a deployment that requirements.txt does not describe.
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
            # Google-brotli streaming API: feed incrementally and stop past the
            # cap. A single process(body) call would materialize the entire
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
