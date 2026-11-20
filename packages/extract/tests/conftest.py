"""In-memory office-format fixture builders.

Builds minimal DOCX/XLSX/PPTX/EPUB containers with zipfile + ElementTree so the
suite never ships binary blobs. Each builder returns raw bytes for extract().
"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from safe_url.host_policy import reset_private_target_memo


@pytest.fixture(autouse=True)
def _reset_private_target_state(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    reset_private_target_memo()


OPC_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
)


def _zip_file(names_to_bytes: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, xml in names_to_bytes.items():
            zf.writestr(name, xml)
    return buffer.getvalue()


def build_docx(paragraphs: list[str]) -> bytes:
    """Minimal DOCX with one paragraph per string in ``paragraphs``."""
    parts = {
        "[Content_Types].xml": OPC_CONTENT_TYPES,
        "word/document.xml": _docx_document(paragraphs),
    }
    return _zip_file(parts)


def _docx_document(paragraphs: list[str]) -> str:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    items = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    return f'<w:document xmlns:w="{ns}"><w:body>{items}</w:body></w:document>'


def build_pptx(slides: list[list[str]]) -> bytes:
    """Minimal PPTX: one slide per entry, each slide a list of shape texts."""
    parts = {"[Content_Types].xml": OPC_CONTENT_TYPES}
    for i, shape_texts in enumerate(slides, 1):
        parts[f"ppt/slides/slide{i}.xml"] = _pptx_slide(shape_texts)
    return _zip_file(parts)


def _pptx_slide(shape_texts: list[str]) -> str:
    ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    items = "".join(f"<p:sp><p:tx><a:bodyPr/><a:t>{text}</a:t></p:tx></p:sp>" for text in shape_texts)
    return f'<p:sld xmlns:p="{ns}">{items}</p:sld>'


def build_xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    """Minimal XLSX: ``sheets`` maps sheet name to rows of cell strings."""
    parts = {"[Content_Types].xml": OPC_CONTENT_TYPES}
    for _name, rows in sheets.items():
        parts["xl/worksheets/sheet.xml"] = _xlsx_sheet(rows)
    return _zip_file(parts)


def _xlsx_sheet(rows: list[list[str]]) -> str:
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rows_xml = ""
    for r_idx, row in enumerate(rows, 1):
        cells = "".join(
            f'<c r="{_col_letter(c_idx)}{r_idx}"><v>{value}</v></c>' for c_idx, value in enumerate(row, 1)
        )
        rows_xml += f'<row r="{r_idx}">{cells}</row>'
    return f'<worksheet xmlns="{ns}"><sheetData>{rows_xml}</sheetData></worksheet>'


def _col_letter(n: int) -> str:
    letter = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letter = chr(65 + rem) + letter
    return letter


def build_epub(spine: list[tuple[str, str]]) -> bytes:
    """Minimal EPUB: ``spine`` is a list of (id, html_content) pairs in order."""
    parts = {
        "META-INF/container.xml": _epub_container(),
        "content.opf": _epub_opf(spine),
    }
    for i, (_, html) in enumerate(spine, 1):
        parts[f"chapter{i}.xhtml"] = html
    return _zip_file(parts)


def _epub_container() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0"'
        ' xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf"'
        ' media-type="application/oebps-package+xml"/></rootfiles></container>'
    )


def _epub_opf(spine: list[tuple[str, str]]) -> str:
    items = "".join(
        f'<item id="{sid}" href="{i + 1}.xhtml" media-type="application/xhtml+xml"/>'
        for i, (sid, _) in enumerate(spine)
    )
    spine_refs = "".join(f'<itemref idref="{sid}"/>' for sid, _ in spine)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        f"<manifest>{items}</manifest>"
        f"<spine>{spine_refs}</spine>"
        "</package>"
    )


def build_pdf_stub(pages: list[str]) -> bytes:
    """Minimal PDF-like container: header, zlib-compressed content streams, EOF.

    Not a real PDF parser target — just a zip-free binary-shaped blob that a
    tier-1 sweep must refuse gracefully (truncated xref, no trailer).
    """
    parts = [b"%PDF-1.4\n"]
    for i, text in enumerate(pages, 1):
        stream = f"{i} 0 obj\n<< /Length {len(text)} >>\nstream\n{text}\nendstream\nendobj\n"
        parts.append(stream.encode("utf-8"))
    parts.append(b"%EOF\n")
    return b"".join(parts)
