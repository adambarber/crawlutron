"""Package skeleton: never-raise, bounded, deterministic extraction."""

from __future__ import annotations

from dataclasses import replace

from crawlutron_extract import (
    DEFAULT_LIMITS,
    Extraction,
    ExtractLimits,
    Quality,
    extract,
)


def test_extract_never_raises_on_hostile_input():
    """Never raises on hostile input: every hostile byte string yields an Extraction."""
    hostile = [
        b"",
        b"\x00\x01\x02",
        b"\xff\xfe\xfd",
        b"<not-closed>",
        b"A" * 10_000_000,
        b"\xed\xa0\x80\xed\xbf\xbf",  # lone surrogates in UTF-8 form
        b"\x00\x00\x00\x00",  # null bytes
    ]
    for payload in hostile:
        result = extract(payload, "text/html")
        assert isinstance(result, Extraction)
        assert result.quality in {Quality.HIGH, Quality.MEDIUM, Quality.LOW, Quality.FAILED}


def test_input_above_max_input_bytes_is_refused():
    """Input larger than max_input_bytes is refused before parsing, quality=failed."""
    limits = ExtractLimits(max_input_bytes=10)
    result = extract(b"x" * 100, "text/html", limits=limits)
    assert result.quality == Quality.FAILED
    assert "input_refused" in result.flags


def test_input_at_exact_cap_is_not_refused():
    """Boundary: input exactly at max_input_bytes is accepted (off-by-one check both sides)."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract(b"hello", "text/html", limits=limits)
    assert result.quality != Quality.FAILED or "input_refused" not in result.flags


def test_input_one_byte_over_cap_is_refused():
    """Boundary: one byte over max_input_bytes is refused."""
    limits = ExtractLimits(max_input_bytes=5)
    result = extract(b"hello!", "text/html", limits=limits)
    assert result.quality == Quality.FAILED
    assert "input_refused" in result.flags


def test_extract_is_deterministic():
    """Same bytes + same limits -> byte-identical Extraction (equality on frozen dataclass)."""
    content = b"<html><body>Hello world</body></html>"
    limits = ExtractLimits()
    first = extract(content, "text/html", limits=limits)
    second = extract(content, "text/html", limits=limits)
    assert first == second


def test_extract_deterministic_with_url():
    content = b"<html><body>Hello</body></html>"
    limits = ExtractLimits()
    a = extract(content, "text/html", url="https://example.com/", limits=limits)
    b = extract(content, "text/html", url="https://example.com/", limits=limits)
    assert a == b


def test_default_limits_are_sane():
    assert DEFAULT_LIMITS.max_input_bytes > 0
    assert DEFAULT_LIMITS.max_text_chars > 0
    assert DEFAULT_LIMITS.max_links > 0


def test_extraction_is_frozen():
    """Frozen dataclass: replace() yields a new instance, original unchanged."""
    e = Extraction()
    e2 = replace(e, text="x")
    assert e2.text == "x"
    assert e.text == ""


def test_extract_limits_is_frozen():
    """Frozen dataclass: replace() yields a new instance, original unchanged."""
    limits = ExtractLimits()
    limits2 = replace(limits, max_text_chars=1)
    assert limits2.max_text_chars == 1
    assert limits.max_text_chars == 100_000


def test_quality_closed_vocabulary():
    """Quality is a closed enum: exactly high/medium/low/failed."""
    assert {q.value for q in Quality} == {"high", "medium", "low", "failed"}


def test_default_extraction_has_empty_payload_and_failed_quality():
    """Empty Extraction has empty text/markdown and quality=failed."""
    e = extract(b"hello", "text/plain")
    assert e.text == ""
    assert e.markdown == ""
    assert e.metadata == {"content_type": "text/plain"}
    assert e.links == []
    assert e.frames == []
    assert e.forms == []
    assert e.tables == []
    assert e.quality == Quality.FAILED
    assert e.flags == []
    assert e.truncated is False


def test_extract_returns_extraction_not_dict():
    """The public door returns an Extraction instance, never a raw dict."""
    result = extract(b"hello", "text/plain")
    assert isinstance(result, Extraction)
    assert not isinstance(result, dict)


def test_extract_accepts_none_url():
    """url=None is accepted (keyword-only optional parameter)."""
    result = extract(b"hello", "text/plain", url=None)
    assert isinstance(result, Extraction)
