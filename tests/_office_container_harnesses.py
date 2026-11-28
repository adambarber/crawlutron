"""In-memory Office container builders shared by the extraction behavior tests."""

from __future__ import annotations

import io
import zipfile

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EPUB_TYPE = "application/epub+zip"


def _make_zip(content_types, rels, main_xml, extra=None, main_path="word/document.xml"):
    """Build a minimal zip container in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr(main_path, main_xml)
        for name, content in extra or []:
            zf.writestr(name, content)
    return buf.getvalue()


def _content_types_xml(overrides):
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


def _make_docx():
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


def _make_pptx():
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


def _make_xlsx(sst_xml: bytes | None = None, sheet_xml: bytes | None = None) -> bytes:
    """Build a minimal XLSX in memory, with optional shared strings/sheet."""
    workbook_xml = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<workbook xmlns="http://schemas.openxmlformats.org/'
        b'spreadsheetml/2006/main">'
        b"<sheets><sheet name='Sheet1' sheetId='1' state='visible'/"
        b"/></sheets>"
        b"</workbook>"
    )
    if sheet_xml is None:
        sheet_xml = (
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
    overrides = [
        (
            b"/xl/workbook.xml",
            b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
        ),
        (
            b"/xl/worksheets/sheet1.xml",
            b"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
        ),
    ]
    extra = [("xl/worksheets/sheet1.xml", sheet_xml)]
    if sst_xml is not None:
        overrides.append(
            (
                b"/xl/sharedStrings.xml",
                b"application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml",
            )
        )
        extra.append(("xl/sharedStrings.xml", sst_xml))
    return _make_zip(
        _content_types_xml(overrides),
        rels,
        workbook_xml,
        extra=extra,
        main_path="xl/workbook.xml",
    )


def _make_epub():
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


def _bomb_zip(compress_type=zipfile.ZIP_STORED):
    """Build a zip whose single 1 MB entry is highly compressible."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", b"A" * 1_000_000, compress_type=compress_type)
    return buf.getvalue()
