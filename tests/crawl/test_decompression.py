"""Response body decompression behavior (no sys.path hacks)."""

import gzip
import unittest
import zlib

from app.crawl.decompression import decompress_body
from app.crawl.strategies.http import _decode_body


class DecompressionTest(unittest.TestCase):
    def test_gzip(self) -> None:
        self.assertEqual(decompress_body(gzip.compress(b"hello world"), "gzip"), b"hello world")

    def test_deflate(self) -> None:
        self.assertEqual(decompress_body(zlib.compress(b"hello world"), "deflate"), b"hello world")

    def test_no_encoding(self) -> None:
        self.assertEqual(decompress_body(b"hello", ""), b"hello")

    def test_decode_body_respects_charset(self) -> None:
        body = "<html>é</html>".encode("utf-8")
        self.assertIn("é", _decode_body(body, "text/html; charset=utf-8"))

    def test_decode_body_handles_unknown_charset(self) -> None:
        body = b"plain"
        self.assertEqual(_decode_body(body, "text/plain"), "plain")

    def test_brotli(self) -> None:
        import brotli

        self.assertEqual(decompress_body(brotli.compress(b"hello brotli"), "br"), b"hello brotli")

    def test_decode_body_empty_body(self) -> None:
        self.assertEqual(_decode_body(b"", "text/html"), "")


if __name__ == "__main__":
    unittest.main()
