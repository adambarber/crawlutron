"""Adversarial fix (HIGH): decompression bomb guard.

A tiny compressed payload that expands to a huge body must be rejected
instead of exhausting memory.
"""

import gzip

import pytest

from app.crawl.decompression import DecompressionTooLargeError, decompress_body


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


def test_decompression_without_limit_is_rejected():
    payload = b"hello world"
    bomb = gzip.compress(payload)
    # H2 closure: no cap is no longer a legal call — the bound is required.
    with pytest.raises(TypeError):
        decompress_body(bomb, "gzip")


def test_deflate_bomb_is_bounded():
    import zlib

    bomb = zlib.compress(b"B" * 1_000_000)
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "deflate", max_output_bytes=10_000)


def test_brotli_bomb_is_bounded():
    # H6: a brotli bomb must raise, not expand unbounded in RAM.
    import brotli

    bomb = brotli.compress(b"C" * 1_000_000)
    assert len(bomb) < 10_000
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "br", max_output_bytes=10_000)


def test_brotli_feeds_incrementally(monkeypatch):
    # H6 mechanism: the google-brotli streaming API must be fed in chunks
    # so the full output is never materialized before the cap is checked.
    # A single process() call with the whole input means unbounded RAM.
    import brotli

    calls: list[int] = []
    real_decompressor = brotli.Decompressor

    class SpyDecompressor(real_decompressor):
        def process(self, data):
            calls.append(len(data))
            return super().process(data)

    monkeypatch.setattr(brotli, "Decompressor", SpyDecompressor)
    import os

    # Incompressible input: the wire form stays large so any reasonable
    # chunking splits it across several process() calls.
    payload = os.urandom(200_000)
    bomb = brotli.compress(payload)
    assert len(bomb) > 65_536
    assert decompress_body(bomb, "br", max_output_bytes=10_000_000) == payload
    assert len(calls) > 1
    assert max(calls) <= 65_536
