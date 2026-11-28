"""Office extraction adversarial tests: DTD/entity refusal, .doc refusal,
zip bombs, truncated archives, duplicate entries, per-member/cumulative caps.
"""

from __future__ import annotations

import io
import zipfile

from crawlutron_extract import ExtractLimits, Quality, extract
from tests._office_container_harnesses import (
    DOCX_TYPE,
    EPUB_TYPE,
    PPTX_TYPE,
    XLSX_TYPE,
    _bomb_zip,
    _content_types_xml,
    _make_docx,
    _make_epub,
    _make_pptx,
    _make_xlsx,
    _make_zip,
)


def test_docx_extract():
    """DOCX extraction should return text from document.xml."""
    result = extract(_make_docx(), DOCX_TYPE)
    assert result.quality == Quality.MEDIUM
    assert "Hello World" in result.text
    assert "Heading One" in result.text
    assert "Cell A1" in result.text
    assert result.metadata["container"] == "docx"


def test_pptx_extract():
    """PPTX extraction should return text from slide XML."""
    result = extract(_make_pptx(), PPTX_TYPE)
    assert result.quality == Quality.MEDIUM
    assert "Slide Title" in result.text
    assert result.metadata["container"] == "pptx"


def test_xlsx_extract():
    """XLSX extraction should return text from sheet XML."""
    result = extract(_make_xlsx(), XLSX_TYPE)
    assert result.quality == Quality.MEDIUM
    assert "Cell A1" in result.text
    assert "Cell B1" in result.text
    assert result.metadata["container"] == "xlsx"


def test_xlsx_numeric_cell_does_not_resolve_as_shared_string():
    """Only t="s" cells consult the shared-string table."""
    sst = b"<sst><si><t>alpha</t></si><si><t>beta</t></si></sst>"
    sheet = (
        b'<worksheet><sheetData><row r="1">'
        b'<c r="A1" t="s"><v>0</v></c>'
        b'<c r="B1"><v>1</v></c>'
        b'<c r="C1" t="inlineStr"><is><t>inline value</t></is></c>'
        b'<c r="D1" t="s"><v>9</v></c>'
        b"</row></sheetData></worksheet>"
    )
    result = extract(_make_xlsx(sst, sheet), XLSX_TYPE)
    tokens = result.text.split()
    assert "alpha" in tokens
    assert "inline" in tokens and "value" in tokens
    assert "1" in tokens, "the numeric cell was misread as a shared-string index"
    assert "9" in tokens, "an out-of-range shared-string index must stay literal"
    assert "beta" not in tokens


def test_epub_extract():
    """EPUB extraction should return text from spine documents."""
    result = extract(_make_epub(), EPUB_TYPE)
    assert result.quality == Quality.MEDIUM
    assert "Chapter 1 text" in result.text
    assert result.metadata["container"] == "epub"


def test_docx_empty_document():
    """DOCX with empty document.xml should yield LOW quality."""
    result = extract(
        _make_zip(
            b'<?xml version="1.0"?><Types/>',
            b"",
            b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
            b'openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        ),
        DOCX_TYPE,
    )
    assert result.quality == Quality.LOW
    assert result.text == ""


def test_doc_refused():
    """.doc legacy binary should be refused with format_refused."""
    result = extract(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100, "application/msword")
    assert result.quality == Quality.LOW
    assert result.text == ""
    assert result.markdown == ""
    assert "msword_unsupported" in result.flags


def test_xml_dtd_refused():
    """XML with DOCTYPE should be refused."""
    result = extract(
        _make_zip(
            b'<?xml version="1.0"?><Types/>',
            b"",
            b'<?xml version="1.0"?><!DOCTYPE foo>'
            b"<w:document><w:body><w:p><w:t>Hello</w:t></w:p></w:body>"
            b"</w:document>",
        ),
        DOCX_TYPE,
    )
    assert result.quality == Quality.FAILED
    assert "xml_dtd_refused" in result.flags


def test_xml_entity_refused():
    """XML with ENTITY declaration should be refused."""
    result = extract(
        _make_zip(
            b'<?xml version="1.0"?><Types/>',
            b"",
            b'<?xml version="1.0"?><!ENTITY xxe "bar">'
            b"<w:document><w:body><w:p><w:t>Hello</w:t></w:p></w:body>"
            b"</w:document>",
        ),
        DOCX_TYPE,
    )
    assert result.quality == Quality.FAILED
    assert "xml_dtd_refused" in result.flags


def test_truncated_archive_refused():
    """Truncated ZIP should be refused."""
    truncated = b"PK\x03\x04" + b"\x00" * 100
    result = extract(truncated, DOCX_TYPE)
    assert result.quality == Quality.FAILED
    assert result.text == ""


def test_duplicate_entries():
    """ZIP with duplicate entries should be handled (last wins)."""
    import warnings

    content = _make_zip(
        b'<?xml version="1.0"?><Types/>',
        b"",
        b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
        b'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        b"<w:p><w:t>First</w:t></w:p></w:body></w:document>",
    )
    buf2 = io.BytesIO(content)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buf2, "a") as zf2:
            zf2.writestr(
                "word/document.xml",
                b'<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
                b'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                b"<w:p><w:t>Second</w:t></w:p></w:body></w:document>",
            )
    result = extract(buf2.getvalue(), DOCX_TYPE)
    assert "Second" in result.text


def test_determinism():
    """Extraction should be deterministic."""
    docx = _make_docx()
    limits = ExtractLimits()
    a = extract(docx, DOCX_TYPE, limits=limits)
    b = extract(docx, DOCX_TYPE, limits=limits)
    assert a == b


def test_pptx_slide_boundaries_and_speaker_notes():
    """PPTX emits slide separators and includes notesSlide text (defect 4)."""
    notes_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        b"<a:t>Speaker note here</a:t>"
        b"</p:notes>"
    )
    content_types = _content_types_xml(
        [
            (
                b"/ppt/slides/slide1.xml",
                b"application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
            )
        ]
    )
    rels = (
        b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
        b'openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        b'officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>'
    )
    slide = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld xmlns:p="http://schemas.'
        b'openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.'
        b'openxmlformats.org/drawingml/2006/main"><p:cSld><p:sp><p:txBody><a:t>Slide Title'
        b"</a:t></p:txBody></p:sp></p:cSld></p:sld>"
    )
    pptx = _make_zip(
        content_types,
        rels,
        slide,
        extra=[("ppt/slides/notesSlide1-notes.xml", notes_xml)],
        main_path="ppt/slides/slide1.xml",
    )
    result = extract(pptx, PPTX_TYPE)
    assert result.quality == Quality.MEDIUM
    assert "[Slide 1]" in result.text
    assert "Slide Title" in result.text
    assert "[Notes]" in result.text
    assert "Speaker note here" in result.text


def test_xlsx_cached_values_preferred_over_formulas():
    """XLSX reads cached <v> values and prefers them over formula strings (defect 5)."""
    sheet = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        b"<sheetData>"
        b'<row r="1"><c r="A1" t="n"><v>99</v><f>A1+B1</f></c>'
        b'<c r="B1"><v>7</v></c></row>'
        b"</sheetData>"
        b"</worksheet>"
    )
    xlsx = _make_zip(
        _content_types_xml(
            [
                (
                    b"/xl/worksheets/sheet1.xml",
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                )
            ]
        ),
        b"",
        b'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.'
        b'openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="Sheet1"'
        b' sheetId="1" state="visible"/></sheets></workbook>',
        extra=[("xl/worksheets/sheet1.xml", sheet)],
        main_path="xl/workbook.xml",
    )
    result = extract(xlsx, XLSX_TYPE)
    assert result.quality == Quality.MEDIUM
    assert "99" in result.text
    assert "7" in result.text


def test_epub_spine_order_skips_nav():
    """EPUB follows spine order and skips properties=nav documents (defect 6)."""
    nav_xml = b'<?xml version="1.0"?><html><body><nav>NAV SHOULD BE SKIPPED</nav></body></html>'
    opf = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        b"<manifest>"
        b'<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        b'<item id="chap1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
        b"</manifest>"
        b"<spine><itemref idref=" + b'"nav"' + b"/><itemref idref=" + b'"chap1"' + b"/></spine>"
        b"</package>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:'
            b'container">'
            b"<rootfiles>"
            b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            b"</rootfiles></container>",
        )
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr(
            "OEBPS/chapter1.xhtml", b'<?xml version="1.0"?><html><body><p>Chapter 1 text</p></body></html>'
        )
        zf.writestr("OEBPS/nav.xhtml", nav_xml)
    result = extract(buf.getvalue(), EPUB_TYPE)
    assert result.quality == Quality.MEDIUM
    assert "Chapter 1 text" in result.text
    assert "NAV SHOULD BE SKIPPED" not in result.text


def test_zip_bomb_is_refused_not_expanded():
    """Zip ratio bomb is refused with FAILED quality (defect 7)."""
    bomb = _bomb_zip(zipfile.ZIP_DEFLATED)
    result = extract(bomb, DOCX_TYPE)
    assert result.quality == Quality.FAILED
    assert result.text == ""
    assert "zip_bomb_refused" in result.flags


def test_zip_traversal_is_refused():
    """A .. zip entry is refused (defect 7)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.xml", b"<w:t>x</w:t>")
    result = extract(buf.getvalue(), DOCX_TYPE)
    assert result.quality == Quality.FAILED and result.text == ""
    assert "zip_traversal_refused" in result.flags


def test_zip_entry_count_cap():
    """Too many zip entries is refused (defect 7)."""
    limits = ExtractLimits(max_zip_entries=5)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(8):
            zf.writestr(f"file{i}.txt", b"x")
    result = extract(buf.getvalue(), DOCX_TYPE, limits=limits)
    assert result.quality == Quality.FAILED and result.text == ""


def test_zip_member_cap_refuses_oversized_member():
    """A member over max_zip_member_bytes is refused."""
    limits = ExtractLimits(max_zip_member_bytes=100, max_zip_inflated_bytes=100_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", b"A" * 200)
    result = extract(buf.getvalue(), DOCX_TYPE, limits=limits)
    assert result.quality == Quality.FAILED
    assert result.text == ""
    assert "zip_member_exceeds_member_cap" in result.flags


def test_zip_cumulative_inflated_cap_refuses():
    """Members under the per-member cap but over the cumulative cap are refused."""
    limits = ExtractLimits(max_zip_member_bytes=2000, max_zip_inflated_bytes=3000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(3):
            zf.writestr(f"ppt/slides/slide{i}.xml", b"<a:t>" + b"x" * 1000 + b"</a:t>")
    result = extract(buf.getvalue(), PPTX_TYPE, limits=limits)
    assert result.quality == Quality.FAILED
    assert result.text == ""
    assert "zip_inflated_exceeds_cumulative_cap" in result.flags
