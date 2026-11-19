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
_WT_PARA_RE = re.compile(r"<w:p[^>]*>.*?</w:p>", re.DOTALL)
_WT_PSTYLE_RE = re.compile(r'<w:pStyle w:val="([^"]+)"')
_WT_TBL_RE = re.compile(r"<w:tbl[^>]*>.*?</w:tbl>", re.DOTALL)
_WT_TR_RE = re.compile(r"<w:tr[^>]*>.*?</w:tr>", re.DOTALL)
_WT_TC_RE = re.compile(r"<w:tc[^>]*>.*?</w:tc>", re.DOTALL)

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
    """Open zip; ``read_members(zf, flags)`` returns extracted text or None on DTD refusal.

    Security: entry-count cap, no ``../`` traversal, duplicate-entry last-wins,
    ratio-bomb refusal.
    """
    zf = _open_zip(content)
    if zf is None:
        return _empty_result("corrupt_container", ["corrupt_container"])

    names = zf.namelist()
    # Entry-count cap
    if len(names) > limits.max_zip_entries:
        return _empty_result("corrupt_container", ["zip_bomb_refused"])
    # Duplicate-entry detection: last entry wins (zipfile default behavior)
    seen: set[str] = set()
    for name in names:
        if name in seen:
            flags.append("zip_duplicate_entry")
        seen.add(name)
        # Path-traversal refusal
        if ".." in name.split("/"):
            return _empty_result("corrupt_container", ["zip_traversal_refused"])
    # Ratio-bomb guard: compressed size vs uncompressed size
    try:
        total_compressed = sum(zf.getinfo(name).compress_size for name in names)
        total_uncompressed = sum(zf.getinfo(name).file_size for name in names)
        if total_compressed > 0 and total_uncompressed / total_compressed > limits.max_zip_ratio:
            return _empty_result("corrupt_container", ["zip_bomb_refused"])
    except Exception:
        flags.append("zip_ratio_unknown")
    text = read_members(zf, flags)
    if text is None:
        return _empty_result("xml_dtd_refused", ["xml_dtd_refused"])
    return _assemble_extraction(text, limits, flags, False, container, fmt)


def _docx_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract text from DOCX document.xml with paragraph structure and tables."""
    xml = _read_zip_member(zf, "word/document.xml")
    if xml is None:
        return ""
    if not _check_no_dtd_or_entities(xml, flags):
        return None
    xml_str = xml.decode("utf-8", errors="replace")
    parts: list[str] = []
    for para_match in _WT_PARA_RE.finditer(xml_str):
        para_xml = para_match.group(0)
        texts = _WT_TEXT_RE.findall(para_xml)
        if not texts:
            continue
        para_text = " ".join(texts)
        pstyle_match = _WT_PSTYLE_RE.search(para_xml)
        if pstyle_match:
            style = pstyle_match.group(1)
            if style == "Heading1":
                parts.append("# " + para_text)
            elif style == "Heading2":
                parts.append("## " + para_text)
            elif style == "Heading3":
                parts.append("### " + para_text)
            else:
                parts.append(para_text)
        else:
            parts.append(para_text)
    # Extract tables
    for tbl_match in _WT_TBL_RE.finditer(xml_str):
        tbl_xml = tbl_match.group(0)
        table_rows: list[str] = []
        for tr_match in _WT_TR_RE.finditer(tbl_xml):
            tr_xml = tr_match.group(0)
            cell_texts: list[str] = []
            for tc_match in _WT_TC_RE.finditer(tr_xml):
                tc_xml = tc_match.group(0)
                tc_texts = _WT_TEXT_RE.findall(tc_xml)
                cell_texts.append(" ".join(tc_texts))
            if cell_texts:
                table_rows.append(" | ".join(cell_texts))
        if table_rows:
            parts.append("[Table] " + " || ".join(table_rows))
    return "\n".join(parts)


def _pptx_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract text from every slide, with slide boundaries and speaker notes."""
    text_parts: list[str] = []
    slide_num = 0
    for name in zf.namelist():
        if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
            slide_xml = _read_zip_member(zf, name)
            if slide_xml is None:
                continue
            if not _check_no_dtd_or_entities(slide_xml, flags):
                return None
            slide_text = slide_xml.decode("utf-8", errors="replace")
            parts = _DRAWING_TEXT_RE.findall(slide_text)
            slide_text_content = " ".join(parts)
            if slide_text_content:
                slide_num += 1
                text_parts.append(f"[Slide {slide_num}]")
                text_parts.append(slide_text_content)
            # Speaker notes
            notes_name = name.replace("/slides/slide", "/slides/notesSlide").replace(".xml", "-notes.xml")
            notes_xml = _read_zip_member(zf, notes_name)
            if notes_xml:
                if not _check_no_dtd_or_entities(notes_xml, flags):
                    return None
                notes_text = _strip_tags(notes_xml)
                if notes_text.strip():
                    text_parts.append("[Notes] " + notes_text.strip())
    return "\n".join(text_parts)


def _xlsx_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract text from every worksheet, using shared strings for cached values."""
    text_parts: list[str] = []

    # Load shared strings
    shared_strings: list[str] = []
    sst_xml = _read_zip_member(zf, "xl/sharedStrings.xml")
    if sst_xml is not None:
        if not _check_no_dtd_or_entities(sst_xml, flags):
            return None
        sst_text = sst_xml.decode("utf-8", errors="replace")
        shared_strings = _SST_TEXT_RE.findall(sst_text)

    for name in zf.namelist():
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            sheet_xml = _read_zip_member(zf, name)
            if sheet_xml is None:
                continue
            if not _check_no_dtd_or_entities(sheet_xml, flags):
                return None
            sheet_text = sheet_xml.decode("utf-8", errors="replace")
            # Extract all cell values: <v> for shared-string refs or inline,
            # <t> for inline strings.
            cell_values: list[str] = []
            for val in re.findall(r"<v>([^<]*)</v>", sheet_text):
                try:
                    idx = int(val)
                    if 0 <= idx < len(shared_strings):
                        cell_values.append(shared_strings[idx])
                    else:
                        cell_values.append(val)
                except ValueError:
                    cell_values.append(val)
            for t in re.findall(r"<t[^>]*>([^<]*)</t>", sheet_text):
                cell_values.append(t)
            if cell_values:
                text_parts.append(" ".join(cell_values))
    return " ".join(text_parts)


def _epub_read(zf: zipfile.ZipFile, flags: list[str]) -> str | None:
    """Extract XHTML text from EPUB spine documents in spine order, excluding nav."""
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

    # Parse spine order
    spine_items = re.findall(r'<itemref[^>]+idref="([^"]+)"', opf_text)

    # Parse manifest to get href for each id, flagging nav documents to skip.
    manifest: dict[str, str] = {}
    nav_ids: set[str] = set()
    for m in re.finditer(r"<item\b[^>]*/?>", opf_text):
        tag = m.group(0)
        id_match = re.search(r'id="([^"]+)"', tag)
        href_match = re.search(r'href="([^"]+)"', tag)
        if not id_match or not href_match:
            continue
        manifest[id_match.group(1)] = href_match.group(1)
        if 'properties="nav"' in tag or "properties='nav'" in tag:
            nav_ids.add(id_match.group(1))

    text_parts: list[str] = []
    for item_id in spine_items:
        if item_id in nav_ids:
            # The navigation document is structure, not content: skip it.
            continue
        href = manifest.get(item_id, "")
        if not href.endswith((".xhtml", ".html", ".htm")):
            continue
        item_path = href
        if not item_path.startswith("/"):
            item_path = opf_dir + "/" + href if opf_dir else href
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
