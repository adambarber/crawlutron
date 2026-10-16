"""Body decompression and decoding: codec selection, caps, bombs, bytearray input and charset fallback."""

import gzip
import zlib
from typing import Any

import brotli
import pytest

from app.transport import decompression as decompression_mod
from app.transport.body_decode import decode_body
from app.transport.decompression import DecompressionTooLargeError, decompress_body, decompress_bounded

_CAP = 10_000_000


def test_cap_is_required_keyword():
    # The uncapped branch existed only as a default; making the cap mandatory turns "forgot to bound a bomb"
    # into a TypeError. The call is made through a mapping of arguments on purpose.
    uncapped: dict[str, Any] = {"body": gzip.compress(b"hello"), "content_encoding": "gzip"}
    with pytest.raises(TypeError):
        decompress_body(**uncapped)


def test_garbage_gzip_is_rejected_under_cap():
    with pytest.raises(decompression_mod.DecompressionError):
        decompress_body(b"not gzip at all", "gzip", max_output_bytes=_CAP)


def test_truncated_gzip_is_rejected():
    body = gzip.compress(b"hello")[:-2]
    with pytest.raises(decompression_mod.DecompressionError):
        decompress_body(body, "gzip", max_output_bytes=100)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(zlib.compress(b"hello world")[:-2], id="truncated"),
        pytest.param(zlib.compress(b"first") + b"junk", id="trailing_junk"),
        pytest.param(zlib.compress(b"first") + zlib.compress(b"second"), id="concatenated"),
    ],
)
def test_deflate_that_is_not_one_stream_is_rejected(body):
    """Truncated, junk-tailed and concatenated deflate are all rejected.

    One body, one claim: a deflate stream that is not a single well-formed
    member has no valid body -- each way to break it is the same rejection.
    """
    with pytest.raises(decompression_mod.DecompressionError):
        decompress_body(body, "deflate", max_output_bytes=100)


def test_concatenated_gzip_members_are_decoded():
    body = gzip.compress(b"first") + gzip.compress(b"second")
    assert decompress_body(body, "gzip", max_output_bytes=100) == b"firstsecond"


def test_decode_body_no_longer_decompresses():
    # Decompression lives at the chokepoint, not inside the text decoder; the headers parameter existed only
    # to double-decompress uncapped.
    with_headers: dict[str, Any] = {
        "body": gzip.compress(b"hello world"),
        "content_type": "text/html",
        "headers": {"content-encoding": "gzip"},
    }
    with pytest.raises(TypeError):
        decode_body(**with_headers)


def test_decode_body_respects_charset():
    body = "<html>é</html>".encode()
    assert "é" in decode_body(body, "text/html; charset=utf-8")


def test_decode_body_handles_unknown_charset():
    assert decode_body(b"plain", "text/plain") == "plain"


def test_brotli():
    assert decompress_body(brotli.compress(b"hello brotli"), "br", max_output_bytes=_CAP) == b"hello brotli"


def test_decode_body_empty_body():
    assert decode_body(b"", "text/html") == ""


def test_decompression_zlib():
    original = b"hello world " * 10
    compressed = zlib.compress(original)
    assert decompress_body(compressed, "deflate", max_output_bytes=10_000_000) == original


def test_decompression_gzip():
    original = b"hello world " * 10
    compressed = gzip.compress(original)
    assert decompress_body(compressed, "gzip", max_output_bytes=10_000_000) == original


def test_http_charset_decode_error():
    from app.transport.body_decode import decode_body

    # Charset rides on the content-type only; a stray value can never reach the decoder as a headers argument
    # (the parameter is gone).
    result = decode_body(b"hello", "text/plain; charset=invalid_charset")
    assert isinstance(result, str)


# -------------------------------------------------------------------------- Stacked Content-Encoding A header
# may name several codings, applied left to right, so decoding runs right to left.


def _stack(*codings: str) -> bytes:
    """Encode ``b"stacked payload"`` with each coding, in the order given."""
    data = b"stacked payload"
    for coding in codings:
        if coding == "gzip":
            data = gzip.compress(data)
        elif coding == "br":
            data = brotli.compress(data)
        else:
            data = zlib.compress(data)
    return data


@pytest.mark.parametrize(
    "codings",
    [
        pytest.param(("gzip", "br"), id="gzip-then-brotli"),
        pytest.param(("br", "gzip"), id="brotli-then-gzip"),
        pytest.param(("gzip", "deflate"), id="gzip-then-deflate"),
    ],
)
def test_stacked_content_encoding_decodes_in_reverse_order(codings):
    assert decompress_body(_stack(*codings), ", ".join(codings), max_output_bytes=_CAP) == b"stacked payload"


def test_a_stacked_layer_still_honours_the_cap():
    """The cap bounds every layer, not just the one that happens to match first."""
    bomb = brotli.compress(gzip.compress(b"A" * 1_000_000))
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "gzip, br", max_output_bytes=10_000)


def test_an_unknown_coding_in_a_stack_passes_the_body_through():
    """Never a half-decoded lie: if any layer is unreadable, the body is returned as-is.

    Raw bytes fail as a parse error the consumer can see; decoding the layers
    it understands would return confidently wrong bytes for a real resource.
    """
    body = _stack("gzip", "br")
    assert decompress_body(body, "gzip, compress", max_output_bytes=_CAP) == body


def test_identity_in_a_stack_is_a_no_op():
    assert decompress_body(gzip.compress(b"hi"), "identity, gzip", max_output_bytes=_CAP) == b"hi"


def test_encoding_tokens_are_case_and_space_insensitive():
    body = _stack("gzip", "br")
    assert decompress_body(body, "  GZIP,\tBr ", max_output_bytes=_CAP) == b"stacked payload"


def test_decode_valid_utf8_strict():
    body = "héllo wörld".encode()
    assert decode_body(body, "text/html; charset=utf-8") == "héllo wörld"


def test_decode_declared_latin1():
    body = "café".encode("latin-1")
    assert decode_body(body, "text/html; charset=latin-1") == "café"


def test_decode_invalid_bytes_does_not_raise():
    # Invalid UTF-8 bytes with a utf-8 declaration must still return a string (via charset-normalizer or the
    # lossy fallback), never raise.
    body = b"\xff\xfe\xc0not valid utf8"
    out = decode_body(body, "text/html; charset=utf-8")
    assert isinstance(out, str)
    assert "not valid utf8" in out


def test_decode_unknown_charset_falls_back():
    # An unknown declared charset must not raise LookupError.
    body = b"plain"
    out = decode_body(body, "text/html; charset=not-a-real-charset")
    assert out == "plain"


def test_decode_handles_bytearray_and_headers():

    from app.transport.decompression import decompress_body

    raw = b"compressed"
    body = gzip.compress(raw)
    decoded_bytes = decompress_body(bytearray(body), "gzip", max_output_bytes=1_000_000)
    out = decode_body(bytearray(decoded_bytes), "text/html")
    assert out == "compressed"


"""Decompress_body must accept bytearray without requiring a bytes copy."""


def test_accepts_bytearray_gzip():

    from app.transport.decompression import decompress_body

    body = bytearray(gzip.compress(b"hello world"))
    out = decompress_body(body, "gzip", max_output_bytes=1024)
    assert out == b"hello world"


def test_accepts_bytearray_brotli():
    from app.transport.decompression import decompress_body

    body = bytearray(brotli.compress(b"<html>br content</html>"))
    out = decompress_body(body, "br", max_output_bytes=1024)
    assert out == b"<html>br content</html>"


def test_accepts_memoryview():
    from app.transport.decompression import decompress_body

    body = memoryview(gzip.compress(b"mv test"))
    out = decompress_body(body, "gzip", max_output_bytes=10_000_000)
    assert out == b"mv test"


def _gzip_bomb(decompressed_size: int) -> bytes:
    # Highly compressible payload -> small on the wire, huge when expanded.
    return gzip.compress(b"A" * decompressed_size)


def test_decompression_raises_when_output_exceeds_limit():
    bomb = _gzip_bomb(1_000_000)  # ~1 MB expanded, ~1 KB on the wire
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "gzip", max_output_bytes=10_000)


def test_bounded_decompressor_reads_only_remaining_cap():
    requested: list[int] = []

    class FakeDecompressor:
        unconsumed_tail = b""
        eof = True
        unused_data = b""

        def decompress(self, data, max_length):
            requested.append(max_length)
            return b"A" * max_length

        def flush(self):
            return b""

    with pytest.raises(DecompressionTooLargeError):
        decompress_bounded(FakeDecompressor, b"payload", 10, allow_concatenated=False)
    assert requested == [11]


def test_decompression_succeeds_under_limit():
    payload = b"A" * 50_000
    bomb = gzip.compress(payload)
    assert decompress_body(bomb, "gzip", max_output_bytes=100_000) == payload


def test_decompression_without_limit_is_rejected():
    payload = b"hello world"
    bomb = gzip.compress(payload)
    # No cap is no longer a legal call — the bound is required.
    uncapped: dict[str, Any] = {"body": bomb, "content_encoding": "gzip"}
    with pytest.raises(TypeError):
        decompress_body(**uncapped)


def test_deflate_bomb_is_bounded():

    bomb = zlib.compress(b"B" * 1_000_000)
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "deflate", max_output_bytes=10_000)


def test_brotli_bomb_is_bounded():
    # A brotli bomb must raise, not expand unbounded in RAM.

    bomb = brotli.compress(b"C" * 1_000_000)
    assert len(bomb) < 10_000
    with pytest.raises(DecompressionTooLargeError):
        decompress_body(bomb, "br", max_output_bytes=10_000)


def test_brotli_feeds_incrementally(monkeypatch):
    # A single process() call with the whole input means unbounded RAM.

    calls: list[int] = []

    # Subclassing ``brotli.Decompressor`` directly, not a variable bound to it: the base class is resolved
    # when this statement runs, and the monkeypatch below replaces the module attribute afterwards, so the spy
    class SpyDecompressor(brotli.Decompressor):
        def process(self, data):
            calls.append(len(data))
            return super().process(data)

    monkeypatch.setattr(brotli, "Decompressor", SpyDecompressor)
    import os

    # Incompressible input: the wire form stays large so any reasonable chunking splits it across several
    # process() calls.
    payload = os.urandom(200_000)
    bomb = brotli.compress(payload)
    assert len(bomb) > 65_536
    assert decompress_body(bomb, "br", max_output_bytes=10_000_000) == payload
    assert len(calls) > 1
    assert max(calls) <= 65_536


class TestDecompressionSelection:
    @pytest.mark.parametrize(
        ("payload", "encoding"),
        [
            (b"not gzip", "gzip"),
            (b"not deflate", "deflate"),
            (b"not brotli", "br"),
        ],
    )
    def test_corrupt_compressed_input_is_rejected(self, payload, encoding):
        """Undecodable bytes are a typed error for every supported codec.

        One row per codec in the selection table states once that a body a codec
        cannot read raises ``DecompressionError``, never a codec-specific exception.
        """
        with pytest.raises(decompression_mod.DecompressionError):
            decompress_body(payload, encoding, max_output_bytes=_CAP)

    def test_plain_passthrough(self):
        assert decompress_body(b"plain", "identity", max_output_bytes=_CAP) == b"plain"
