import pytest

"""Adversarial fix (HIGH): decompression bomb guard.

A tiny compressed payload that expands to a huge body must be rejected
instead of exhausting memory.
"""
import gzip

from app.crawl.decompression import decompress_body, DecompressionTooLargeError


def _gzip_bomb(decompressed_size: int) -> bytes:
    # Highly compressible payload -> small on the wire, huge when expanded.
    return gzip.compress(b"A" * decompressed_size)


def test_decompression_raises_when_output_exceeds_limit():
    bomb = _gzip_bomb(1_000_000)  # ~1 MB expanded, ~1 KB on the wire
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "gzip", max_output_bytes=10_000)


def test_decompression_succeeds_under_limit():
    payload = b"A" * 50_000
    bomb = gzip.compress(payload)
    assert decompress_body(bomb, "gzip", max_output_bytes=100_000) == payload


def test_decompression_without_limit_is_unchanged():
    payload = b"hello world"
    bomb = gzip.compress(payload)
    # Backward-compatible: no max_output_bytes -> decompress normally.
    assert decompress_body(bomb, "gzip") == payload


def test_deflate_bomb_is_bounded():
    import zlib
    bomb = zlib.compress(b"B" * 1_000_000)
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "deflate", max_output_bytes=10_000)
