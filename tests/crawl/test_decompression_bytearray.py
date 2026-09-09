"""decompress_body must accept bytearray without requiring a bytes copy."""


def test_accepts_bytearray_gzip():
    import gzip

    from app.crawl.decompression import decompress_body, detect_decompression_failure

    body = bytearray(gzip.compress(b"hello world"))
    out = decompress_body(body, "gzip", max_output_bytes=1024)
    assert out == b"hello world"
    assert detect_decompression_failure(bytes(body), "gzip", out) is False


def test_accepts_bytearray_brotli():
    import brotli

    from app.crawl.decompression import decompress_body

    body = bytearray(brotli.compress(b"<html>br content</html>"))
    out = decompress_body(body, "br", max_output_bytes=1024)
    assert out == b"<html>br content</html>"


def test_accepts_memoryview():
    from app.crawl.decompression import decompress_body

    body = memoryview(gzip.compress(b"mv test")) if (gzip := __import__("gzip")) else None
    out = decompress_body(body, "gzip")
    assert out == b"mv test"
