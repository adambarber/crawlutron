"""Golden corpus walk: every committed reference file extracts safely and deterministically."""

from __future__ import annotations

from pathlib import Path

from crawlutron_extract import DEFAULT_LIMITS, extract
from crawlutron_extract.models import Quality

GOLDEN = Path(__file__).parent / "golden"

CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".test": "text/html",
    ".dat": "text/html",
}

FAMILIES: dict[str, tuple[tuple[str, str], ...]] = {
    "html": (
        ("tokenizer_test1.test", "text/html"),
        ("tokenizer_escapeFlag.test", "text/html"),
        ("tokenizer_xmlViolation.test", "text/html"),
        ("tokenizer_unicodeCharsProblematic.test", "text/html"),
        ("encoding_tests2.dat", "text/html"),
        ("encoding_test-yahoo-jp.dat", "text/html"),
    ),
    "pdf": (
        ("simple1.pdf", "application/pdf"),
        ("simple3.pdf", "application/pdf"),
        ("test_pattern_colors.pdf", "application/pdf"),
        ("sampleOneByteIdentityEncode.pdf", "application/pdf"),
    ),
    "docx": (
        ("55733.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("52288.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ),
    "xlsx": (
        ("1_NoIden.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("123233_charts.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ),
    "pptx": (
        (
            "49386-null_dates.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    ),
    "md": (("LICENSES.md", "text/markdown"),),
}


def _all_cases() -> list[tuple[str, str]]:
    return [(name, ct) for names in FAMILIES.values() for name, ct in names]


def _read(name: str) -> bytes:
    return (GOLDEN / name).read_bytes()


def _extract(name: str, content_type: str):
    return extract(_read(name), content_type, limits=DEFAULT_LIMITS)


def test_golden_never_raises() -> None:
    """Every corpus file must extract without raising, even on hostile/binary input."""
    for name, content_type in _all_cases():
        result = _extract(name, content_type)
        assert result is not None


def test_golden_output_bounded_by_limits() -> None:
    """Every output must stay inside the configured caps."""
    for name, content_type in _all_cases():
        result = _extract(name, content_type)
        assert len(result.text) <= DEFAULT_LIMITS.max_text_chars
        assert len(result.markdown) <= DEFAULT_LIMITS.max_markdown_chars
        assert len(result.links) <= DEFAULT_LIMITS.max_links
        assert len(result.tables) <= DEFAULT_LIMITS.max_tables
        assert len(result.frames) <= DEFAULT_LIMITS.max_frames
        assert len(result.forms) <= DEFAULT_LIMITS.max_forms


def test_golden_deterministic_across_two_calls() -> None:
    """Two calls on the same bytes must produce byte-identical extractions."""
    for name, content_type in _all_cases():
        data = _read(name)
        first = extract(data, content_type, limits=DEFAULT_LIMITS)
        second = extract(data, content_type, limits=DEFAULT_LIMITS)
        assert first == second


def test_golden_no_content_is_not_above_low() -> None:
    """A file yielding no extractable content must not rank above quality=low."""
    for name, content_type in _all_cases():
        result = _extract(name, content_type)
        has_content = (
            bool(result.text.strip()) or result.links or result.tables or result.frames or result.forms
        )
        if not has_content:
            assert result.quality in (Quality.LOW, Quality.FAILED), (name, result.quality)


# One pinned expectation per format family (observed from the live extractor).


def test_golden_html_pins_doctype_description() -> None:
    """Tokenizer vector: the first DOCTYPE description survives extraction."""
    result = _extract("tokenizer_test1.test", "text/html")
    assert "Correct Doctype lowercase" in result.text
    assert result.quality == Quality.MEDIUM
    assert result.charset == "utf-8"


def test_golden_pdf_pins_hello_world() -> None:
    """PDF tier-1 sweep yields the "Hello World" body text."""
    result = _extract("simple1.pdf", "application/pdf")
    assert "Hello World" in result.text
    assert result.quality == Quality.MEDIUM


def test_golden_docx_pins_test() -> None:
    """DOCX single-paragraph document yields exactly "TEST"."""
    result = _extract("55733.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert result.text == "TEST"
    assert result.quality == Quality.MEDIUM


def test_golden_xlsx_pins_korean_body() -> None:
    """XLSX cached values yield the Korean body text."""
    result = _extract("1_NoIden.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert result.text.startswith("가나다")
    assert result.quality == Quality.MEDIUM


def test_golden_pptx_pins_slide_title() -> None:
    """PPTX yields the slide title with the slide boundary marker."""
    result = _extract(
        "49386-null_dates.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert result.text == "[Slide 1]\nTitle"
    assert result.quality == Quality.MEDIUM


def test_golden_empty_pdf_is_low_not_higher() -> None:
    """PDF with no extractable text ranks low, never medium/high."""
    result = _extract("test_pattern_colors.pdf", "application/pdf")
    assert result.text == ""
    assert result.quality == Quality.LOW


def test_golden_md_pins_licenses_header() -> None:
    """LICENSES.md yields its heading as non-empty text."""
    result = _extract("LICENSES.md", "text/markdown")
    assert result.text.strip()
    assert "Golden corpus provenance" in result.text


def test_golden_docx_52288_pins_chapter_one() -> None:
    """52288.docx yields its chapter body as non-empty text."""
    result = _extract(
        "52288.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.text.strip()
    assert result.text.startswith("CHAPTER 1")
    assert result.quality == Quality.MEDIUM


def test_golden_xlsx_charts_is_empty_low() -> None:
    """Charts-only xlsx yields no text and ranks low, never medium/high."""
    result = _extract(
        "123233_charts.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert result.text == ""
    assert result.quality == Quality.LOW
