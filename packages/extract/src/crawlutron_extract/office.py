"""Office container extraction (DOCX, PPTX, XLSX, EPUB) via zip+XML.

Regex-based XML parsing; DTD/DOCTYPE refused; outputs bounded by limits.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable

from crawlutron_extract.limits import ExtractLimits, _empty_result

__all__ = ["extract_office"]

#: DOCTYPE must be refused: never expand external entities (XML bomb).
_DOCTYPE_RE = re.compile(r"<!DOCTYPE\s", re.IGNORECASE)

#: XML entity declarations must be refused.
_ENTITY_DECL_RE = re.compile(r"<!ENTITY\s", re.IGNORECASE)

#: WordprocessingML text elements.
_WT_TEXT_RE = re.compile(r"<w:t[^>]*>([^<]*)</w:t>")

#: DrawingML text elements.
_DRAWING_TEXT_RE = re.compile(r"<a:t[^>]*>([^<]*)</a:t>")

#: SpreadsheetML inline strings and shared strings.
_SST_TEXT_RE = re.compile(r"<t[^>]*>([^<]*)</t>")
_SI_TEXT_RE = re.compile(r"<si[^>]*>(.*?)</si>", re.DOTALL)

#: XHTML text extraction (for EPUB).
_XHTML_TEXT_RE = re.compile(r">([^<]*)<")


def _open_zip(content: bytes) -> zipfile.ZipFile | None:
    """Open a zip file from bytes, returning None if corrupt."""
    try:
        return zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError):
        return None


def _read_zip_member(zf: zipfile.ZipFile, name: str) -> bytes | None:
    """Read a zip member, returning None if not present."""
    try:
        return zf.read(name)
    except KeyError:
        return None


def _has_dtd_or_entities(content: bytes) -> bool:
    """Check for DOCTYPE or entity declarations in XML content."""
    text = content.decode("utf-8", errors="replace")
    return bool(_DOCTYPE_RE.search(text) or _ENTITY_DECL_RE.search(text))


def _check_no_dtd_or_entities(content: bytes, flags: list[str]) -> bool:
    """Refuse DOCTYPE/entity content; return False when refused, True otherwise."""
    if _has_dtd_or_entities(content):
        flags.append("xml_dtd_refused")
        return False
    return True


def _apply_caps(text: str, limits: ExtractLimits, flags: list[str]) -> tuple[str, bool]:
    """Cap text at max_text_chars, recording truncation."""
    truncated = False
    if len(text) > limits.max_text_chars:
        text = text[: limits.max_text_chars]
        flags.append("text_capped")
        truncated = True
    return text, truncated


def _strip_tags(xml_content: bytes) -> str:
    """Strip XML declaration, processing instructions, comments, tags; collapse whitespace."""
    text = xml_content.decode("utf-8", errors="replace")
    text = re.sub(r"<\?xml[^>]*\?>", " ", text)
    text = re.sub(r"<\?[^>]*\?>", " ", text)
    text = re.sub(r"<!--[^>]*-->", " ", text)
    text = re.sub(r"<[^>]*>", " ", text)
    return " ".join(text.split())


def _zip_frame(
    content: bytes,
    limits: ExtractLimits,
    flags: list[str],
    read_members: Callable[[zipfile.ZipFile, list[str]], str | None],
    container: str,
    fmt: str,
) -> dict:
    """Open zip; ``read_members(zf, flags)`` returns extracted text or None on DTD refusal."""
    zf = _open_zip(content)
    if zf is None:
        return _empty_result("corrupt_container", ["corrupt_container"])
    text = read_members(zf, flags)
    if text is None:
        return _empty_result("xml_dtd_refused", ["xml_dtd_refused"])
    return _assemble_extraction(text, limits, flags, False, container, fmt)


def _docx_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract w:t text runs from word/document.xml."""
    xml = _read_zip_member(zf, "word/document.xml")
    if xml is None:
        return ""
    if not _check_no_dtd_or_entities(xml, flags):
        return None
    return " ".join(_WT_TEXT_RE.findall(xml.decode("utf-8", errors="replace")))


def _pptx_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract a:t text runs from every slide member."""
    text_parts: list[str] = []
    for name in zf.namelist():
        if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
            slide_xml = _read_zip_member(zf, name)
            if slide_xml is None:
                continue
            if not _check_no_dtd_or_entities(slide_xml, flags):
                return None
            slide_text = slide_xml.decode("utf-8", errors="replace")
            parts = _DRAWING_TEXT_RE.findall(slide_text)
            text_parts.extend(parts)
    return " ".join(text_parts)


def _xlsx_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract t text runs from every worksheet member."""
    text_parts: list[str] = []
    for name in zf.namelist():
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            sheet_xml = _read_zip_member(zf, name)
            if sheet_xml is None:
                continue
            if not _check_no_dtd_or_entities(sheet_xml, flags):
                return None
            sheet_text = sheet_xml.decode("utf-8", errors="replace")
            parts = _SST_TEXT_RE.findall(sheet_text)
            text_parts.extend(parts)
    return " ".join(text_parts)


def _epub_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract XHTML text from EPUB spine documents in manifest order."""
    container_xml = _read_zip_member(zf, "META-INF/container.xml")
    if container_xml is None:
        return ""
    if not _check_no_dtd_or_entities(container_xml, flags):
        return None
    opf_match = re.search(
        r'rootfile[^>]+full-path="([^"]+)"', container_xml.decode("utf-8", errors="replace")
    )
    if not opf_match:
        return ""
    opf_path = opf_match.group(1)
    opf_dir = opf_path.rsplit("/", 1)[0] if "/" in opf_path else ""
    opf_content = _read_zip_member(zf, opf_path)
    if opf_content is None:
        return ""
    if not _check_no_dtd_or_entities(opf_content, flags):
        return None
    opf_text = opf_content.decode("utf-8", errors="replace")
    manifest_items = re.findall(r'<item[^>]+href="([^"]+)"[^>]*>', opf_text)
    text_parts: list[str] = []
    for item in manifest_items:
        if not item.endswith((".xhtml", ".html", ".htm")):
            continue
        item_path = item
        if not item.startswith("/"):
            item_path = opf_dir + "/" + item if opf_dir else item
        item_content = _read_zip_member(zf, item_path.lstrip("/"))
        if item_content is None:
            continue
        if not _check_no_dtd_or_entities(item_content, flags):
            return None
        item_text = _strip_tags(item_content)
        text_parts.append(item_text)
    return " ".join(text_parts)


def extract_office(
    content: bytes,
    content_type: str,
    limits: ExtractLimits,
) -> dict:
    """Extract text from office containers (DOCX/PPTX/XLSX/EPUB). Never raises.

    Returns a dict with text, markdown, metadata, links, frames, forms,
    tables, truncated, found_main, quality_flags, _quality.
    """
    ctype = content_type.lower()
    flags: list[str] = []
    if ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        return _zip_frame(content, limits, flags, _docx_read, "docx", "wordprocessingml.document")
    if ctype == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        return _zip_frame(content, limits, flags, _pptx_read, "pptx", "presentationml.presentation")
    if ctype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return _zip_frame(content, limits, flags, _xlsx_read, "xlsx", "spreadsheetml.sheet")
    if ctype == "application/epub+zip":
        return _zip_frame(content, limits, flags, _epub_read, "epub", "epub+zip")
    # .doc legacy binary and other unrecognized containers.
    return _empty_result("unsupported_container", ["format_refused"])


def _assemble_extraction(
    text: str,
    limits: ExtractLimits,
    flags: list[str],
    truncated: bool,
    container: str,
    fmt: str,
) -> dict:
    """Build the standard extraction result dict."""
    text, truncated = _apply_caps(text, limits, flags)

    metadata = {"container": container, "format": fmt}
    quality = "medium" if text.strip() else "low"

    return {
        "text": text,
        "markdown": text,
        "metadata": metadata,
        "links": [],
        "frames": [],
        "forms": [],
        "tables": [],
        "truncated": truncated,
        "found_main": False,
        "quality_flags": flags,
        "_quality": quality,
    }
