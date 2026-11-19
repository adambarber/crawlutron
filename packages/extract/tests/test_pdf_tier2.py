"""PDF tier-2 (pdfminer.six fallback) behavior through the public extract door."""

from __future__ import annotations

from crawlutron_extract import ExtractLimits, Quality, extract

LIMITS = ExtractLimits()


def _pdf(name: str) -> bytes:
    with open(f"packages/extract/tests/fixtures/{name}", "rb") as fh:
        return fh.read()


def _assert_below_floor(result) -> None:
    """Tier-1 result kept: low quality, empty text, partial flag, no tier-2."""
    assert result.quality == Quality.LOW
    assert result.text == ""
    assert "pdf_encoding_partial" in result.flags
    assert "pdf_tier2" not in result.flags


def test_pdf_tier2_rescues_subset_font():
    """jo.pdf has no usable tier-1 text; tier-2 recovers the CJK content."""
    result = extract(_pdf("jo.pdf"), "application/pdf", limits=LIMITS)
    assert result.quality != Quality.LOW
    assert result.text
    assert "み" in result.text
    assert "pdf_tier2" in result.flags


def test_pdf_tier2_corrupt_stream_recovers_text():
    """A corrupt stream keeps tier-1 below the floor; tier-2 recovers Zen text."""
    result = extract(_pdf("zen_of_python_corrupted.pdf"), "application/pdf", limits=LIMITS)
    assert result.quality != Quality.LOW
    assert "Beautiful is better than" in result.text
    assert "pdf_tier2" in result.flags


def test_pdf_tier2_no_text_page_stays_low():
    """A page with no recoverable text stays low and emits nothing garbled."""
    result = extract(_pdf("simple2.pdf"), "application/pdf", limits=LIMITS)
    _assert_below_floor(result)


def test_pdf_tier2_total_failure_keeps_tier1():
    """When pdfminer cannot parse the input at all, tier-1 result is kept."""
    result = extract(b"this is not a pdf at all", "application/pdf", limits=LIMITS)
    _assert_below_floor(result)


def test_pdf_table_reconstruction_emits_gfm():
    """Tier-2 reconstructs the table as GFM rows; cell text survives."""
    result = extract(_pdf("sample_table.pdf"), "application/pdf", limits=LIMITS)
    assert "pdf_tier2" in result.flags
    assert "|" in result.text
    for cell in ("Name", "Salary", "Alice", "Engineering"):
        assert cell in result.text


def test_pdf_table_degraded_sets_flag_without_garbled_output():
    """Misaligned columns cannot form a grid: degraded flag, no GFM table."""
    result = extract(_pdf("degraded_table.pdf"), "application/pdf", limits=LIMITS)
    assert "pdf_table_degraded" in result.flags
    assert "pdf_tier2" in result.flags
    assert "|" not in result.text
    assert result.quality != Quality.LOW
