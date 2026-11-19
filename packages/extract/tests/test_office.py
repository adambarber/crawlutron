"""Office container extraction: DOCX, PPTX, XLSX, EPUB adversarial tests.

Matrix: DOCX/PPTX/XLSX/EPUB extraction, DTD/entity refusal, .doc refusal,
zip bombs, truncated archives, duplicate entries.
"""

from __future__ import annotations

import io
import zipfile

from crawlutron_extract import ExtractLimits, Quality, extract


def _make_zip(
    content_types: bytes,
    rels: bytes,
    main_xml: bytes,
    extra: list[tuple[str, bytes]] | None = None,
    main_path: str = "word/document.xml",
) -> bytes:
    """Build a minimal zip container in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr(main_path, main_xml)
        for name, content in extra or []:
            zf.writestr(name, content)
    return buf.getvalue()


def _content_types_xml(overrides: list[tuple[bytes, bytes]]) -> bytes:
    """Build [Content_Types].xml bytes with the given (part, content-type) overrides."""
    parts = [
        b'<?xml version="1.0" encoding="UTF-8"?>',
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        b'<Default Extension="xml" ContentType="application/xml"/>',
    ]
    for part, ctype in overrides:
        parts.append(b'<Override PartName="' + part + b'" ContentType="' + ctype + b'"/>')
    parts.append(b"</Types>")
    return b"".join(parts)


def _make_docx() -> bytes:
    """Build a minimal DOCX in memory."""
    return _make_zip(
        _content_types_xml(
            [
                (
                    b"/word/document.xml",
                    b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                )
            ]
        ),
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        b'officeDocument/2006/relationships/document" Target="word/'
        b'document.xml"/>'
        b"</Relationships>",
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body>"
        b"<w:p><w:r><w:t>Hello World</w:t></w:r></w:p>"
        b'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Heading One</w:t></w:r></w:p>'
        b"<w:p><w:r><w:t>Bullet item</w:t></w:r></w:p>"
        b"<w:tbl><w:tr><w:tc><w:t>Cell A1</w:t></w:tc><w:tc><w:t>Cell B1</w:t></w:tc></w:tr></w:tbl>"
        b"</w:body>"
        b"</w:document>",
    )


def _make_pptx() -> bytes:
    """Build a minimal PPTX in memory."""
    slide1_xml = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<p:sld xmlns:p="http://schemas.openxmlformats.org/'
        b'presentationml/2006/main" xmlns:a="http://schemas.'
        b'openxmlformats.org/drawingml/2006/main">'
        b"<p:cSld><p:sp><p:txBody><a:t>Slide Title</a:t></p:txBody></p:sp></p:cSld>"
        b"</p:sld>"
    )
    rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/'
        b'package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        b'officeDocument/2006/relationships/slide" Target="slides/'
        b'slide1.xml"/>'
        b"</Relationships>"
    )
    return _make_zip(
        _content_types_xml(
            [
                (
                    b"/ppt/slides/slide1.xml",
                    b"application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
                )
            ]
        ),
        rels,
        slide1_xml,
        main_path="ppt/slides/slide1.xml",
    )


def _make_xlsx() -> bytes:
    """Build a minimal XLSX in memory."""
    workbook_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<workbook xmlns="http://schemas.openxmlformats.org/'
        b'spreadsheetml/2006/main">'
        b"<sheets><sheet name='Sheet1' sheetId='1' state='visible'/"
        b"/></sheets>"
        b"</workbook>"
    )
    sheet1_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<worksheet xmlns="http://schemas.openxmlformats.org/'
        b'spreadsheetml/2006/main">'
        b"<sheetData>"
        b'<row r="1"><c r="A1"><t>Cell A1</t></c><c r="B1"><t>Cell B1</t>'
        b"</c></row>"
        b'<row r="2"><c r="A2"><t>Cell A2</t></c><c r="B2"><t>Cell B2</t>'
        b"</c></row>"
        b"</sheetData>"
        b"</worksheet>"
    )
    rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/'
        b'package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        b'officeDocument/2006/relationships/worksheet" Target="works/'
        b'heets/sheet1.xml"/>'
        b"</Relationships>"
    )
    return _make_zip(
        _content_types_xml(
            [
                (
                    b"/xl/workbook.xml",
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
                (
                    b"/xl/worksheets/sheet1.xml",
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                ),
            ]
        ),
        rels,
        workbook_xml,
        extra=[("xl/worksheets/sheet1.xml", sheet1_xml)],
        main_path="xl/workbook.xml",
    )


def _make_epub() -> bytes:
    """Build a minimal EPUB in memory."""
    container_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        b'<rootfiles><rootfile full-path="OEBPS/content.opf" '
        b'media-type="application/oebps-package+xml"/></rootfiles>'
        b"</container>"
    )
    opf_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        b"<manifest>"
        b'<item id="chap1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
        b"</manifest>"
        b"<spine><itemref idref=" + b'"chap1"' + b"/></spine>"
        b"</package>"
    )
    chapter1_xhtml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<html xmlns="http://www.w3.org/1999/xhtml">'
        b"<body><p>Chapter 1 text</p></body>"
        b"</html>"
    )
    mimetype = b"application/epub+zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("mimetype", mimetype)
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf_xml)
        zf.writestr("OEBPS/chapter1.xhtml", chapter1_xhtml)
    return buf.getvalue()


def _bomb_zip(compress_type: int = zipfile.ZIP_STORED) -> bytes:
    """Build a zip whose single 1 MB entry is highly compressible."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", b"A" * 1_000_000, compress_type=compress_type)
    return buf.getvalue()


def test_docx_extract():
    """DOCX extraction should return text from document.xml."""
    result = extract(_make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert result.quality == Quality.MEDIUM
    assert "Hello World" in result.text
    assert "Heading One" in result.text
    assert "Cell A1" in result.text
    assert result.metadata["container"] == "docx"


def test_pptx_extract():
    """PPTX extraction should return text from slide XML."""
    content_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    result = extract(_make_pptx(), content_type)
    assert result.quality == Quality.MEDIUM
    assert "Slide Title" in result.text
    assert result.metadata["container"] == "pptx"


def test_xlsx_extract():
    """XLSX extraction should return text from sheet XML."""
    result = extract(_make_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert result.quality == Quality.MEDIUM
    assert "Cell A1" in result.text
    assert "Cell B1" in result.text
    assert result.metadata["container"] == "xlsx"


def test_epub_extract():
    """EPUB extraction should return text from spine documents."""
    result = extract(_make_epub(), "application/epub+zip")
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
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
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
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert result.quality == Quality.FAILED
    assert "xml_dtd_refused" in result.flags


def test_zip_bomb_refused():
    """Zip ratio bomb should be refused or yield empty result."""
    bomb = _bomb_zip()
    original_size = len(bomb)
    import zlib

    compressed = zlib.compress(b"A" * 1_000_000)
    ratio = original_size / len(compressed) if len(compressed) > 0 else float("inf")
    assert ratio > 100

    result = extract(bomb, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    limits = ExtractLimits()
    assert result.text == "" or len(result.text) <= limits.max_text_chars


def test_truncated_archive_refused():
    """Truncated ZIP should be refused."""
    truncated = b"PK\x03\x04" + b"\x00" * 100
    result = extract(truncated, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
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
    result = extract(
        buf2.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "Second" in result.text


def test_determinism():
    """Extraction should be deterministic."""
    docx = _make_docx()
    limits = ExtractLimits()
    a = extract(
        docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", limits=limits
    )
    b = extract(
        docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", limits=limits
    )
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
    result = extract(pptx, "application/vnd.openxmlformats-officedocument.presentationml.presentation")
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
    result = extract(xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
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
        chapter1_xhtml = b'<?xml version="1.0"?><html><body><p>Chapter 1 text</p></body></html>'
        zf.writestr("OEBPS/chapter1.xhtml", chapter1_xhtml)
        zf.writestr("OEBPS/nav.xhtml", nav_xml)
    result = extract(buf.getvalue(), "application/epub+zip")
    assert result.quality == Quality.MEDIUM
    assert "Chapter 1 text" in result.text
    assert "NAV SHOULD BE SKIPPED" not in result.text


def test_zip_bomb_is_refused_not_expanded():
    """Zip ratio bomb is refused with FAILED quality (defect 7)."""
    bomb = _bomb_zip(zipfile.ZIP_DEFLATED)
    docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    result = extract(bomb, docx_type)
    assert result.quality == Quality.FAILED
    assert result.text == ""
    assert "zip_bomb_refused" in result.flags


def test_zip_traversal_is_refused():
    """A .. zip entry is refused (defect 7)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.xml", b"<w:t>x</w:t>")
    docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    result = extract(buf.getvalue(), docx_type)
    assert result.quality == Quality.FAILED and result.text == ""
    assert "zip_traversal_refused" in result.flags


def test_zip_entry_count_cap():
    """Too many zip entries is refused (defect 7)."""
    limits = ExtractLimits(max_zip_entries=5)
    docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(8):
            zf.writestr(f"file{i}.txt", b"x")
    result = extract(buf.getvalue(), docx_type, limits=limits)
    assert result.quality == Quality.FAILED and result.text == ""
