"""Schema-driven CSS-selector field extraction from HTML.

Defines FieldSpec, Schema, extract_records(); declares lxml, pdfminer, safe-url.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from crawlutron_extract.css_select import (
    SelectorError,
    find_all_any,
    parse_selector_list,
)
from crawlutron_extract.html_dom import DOMNode, build_dom
from crawlutron_extract.html_text import render_markdown
from crawlutron_extract.limits import ExtractLimits
from crawlutron_extract.models import Extraction, Quality
from safe_url import HostPolicy, admit_or_none


class FieldKind(StrEnum):
    """Closed vocabulary of field kinds."""

    TEXT = "text"
    MARKDOWN = "markdown"
    ATTR = "attr"
    LINK = "link"
    NUMBER = "number"
    INTEGER = "integer"


@dataclass(frozen=True)
class FieldSpec:
    """Declaration of a single extracted field."""

    name: str
    selector: str
    kind: FieldKind = FieldKind.TEXT
    attribute: str | None = None
    required: bool = False
    many: bool = False


@dataclass(frozen=True)
class Schema:
    """Schema for record-based extraction.

    record selector, fields; None record means the whole document.
    """

    record: str | None
    fields: tuple[FieldSpec, ...]

    def __post_init__(self) -> None:
        """Refuse duplicate field names at construction, naming both fields."""
        seen: set[str] = set()
        for field in self.fields:
            if field.name in seen:
                raise ValueError(f"Duplicate field name {field.name!r} in schema")
            seen.add(field.name)


def _resolve_url(base_url: str | None, href: str) -> str | None:
    """Resolve a possibly-relative href against the document base URL."""
    if not href:
        return None
    if href.startswith(("http://", "https://")):
        return href
    if base_url is None:
        return href
    from urllib.parse import urljoin

    return urljoin(base_url, href)


def _find_base_href(node: DOMNode) -> str | None:
    """Return the first document ``<base href>`` value, if any."""
    if node.tag == "base":
        return node.get_attribute("href")
    for child in node.children:
        href = _find_base_href(child)
        if href is not None:
            return href
    return None


def _coerce_number(value: str) -> float | None:
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _coerce_integer(value: str) -> int | None:
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _extract_field_value(
    node,
    field: FieldSpec,
    base_url: str | None,
    policy: HostPolicy,
) -> object | None:
    """Extract a single field value from a matched node."""
    if field.kind == FieldKind.TEXT:
        return node.get_text()
    elif field.kind == FieldKind.MARKDOWN:
        return render_markdown(node)
    elif field.kind == FieldKind.ATTR:
        return node.get_attribute(field.attribute)
    elif field.kind == FieldKind.LINK:
        href = node.get_attribute(field.attribute or "href")
        if href is None:
            return None
        resolved = _resolve_url(base_url, href)
        if resolved is None:
            return None
        admitted = admit_or_none(resolved, policy=policy)
        return str(admitted) if admitted is not None else None
    elif field.kind == FieldKind.NUMBER:
        return _coerce_number(node.get_text())
    elif field.kind == FieldKind.INTEGER:
        return _coerce_integer(node.get_text())
    assert_never(field.kind)


def extract_records(
    html: str | bytes,
    schema: Schema,
    *,
    url: str | None = None,
    limits: ExtractLimits | None = None,
) -> Extraction:
    """Extract structured records from HTML using a schema.

    Bounded, deterministic, never raises. Returns an Extraction with
    records populated and appropriate quality/flags.
    """
    if limits is None:
        limits = ExtractLimits()

    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    dom, _ = build_dom(html, limits.max_dom_nodes, limits.max_dom_depth)
    if dom is None:
        return Extraction(
            quality=Quality.FAILED,
            flags=["schema_dom_cap"],
            truncated=True,
        )

    # Parse and validate all selectors first
    field_selectors: list[list[tuple[list[tuple[str, ...]], str | None]]] = []
    record_selector_chain: list[tuple[list[tuple[str, ...]], str | None]] | None = None

    try:
        if schema.record is not None:
            record_selector_chain = parse_selector_list(schema.record)[0]
        for field in schema.fields:
            field_selectors.append(parse_selector_list(field.selector)[0])
    except SelectorError:
        return Extraction(
            quality=Quality.FAILED,
            flags=["selector_invalid"],
            truncated=False,
        )

    # Find record nodes
    if schema.record is None:
        record_nodes = [dom]
    else:
        record_nodes = find_all_any(dom, [record_selector_chain])

    records: list[dict] = []
    records_truncated = False
    fields_truncated = False
    schema_required_missing = False

    # A document <base href> rebases relative URLs, as extract_discovered_links
    # does; LINK values always admit through safe_url (it refuses unsafe schemes).
    policy = HostPolicy.unrestricted()
    base_href = _find_base_href(dom)
    document_base = _resolve_url(url, base_href) if base_href else url

    for record_node in record_nodes:
        if len(records) >= limits.max_records:
            records_truncated = True
            break
        record: dict[str, object] = {}
        fields_added = 0
        for field_idx, field in enumerate(schema.fields):
            if fields_added >= limits.max_fields_per_record:
                fields_truncated = True
                break
            matches = find_all_any(record_node, [field_selectors[field_idx]])

            if not matches:
                if field.required:
                    schema_required_missing = True
                continue

            target_nodes = matches if field.many else [matches[0]]

            values: list[object] = []
            if len(target_nodes) > limits.max_fields_per_record:
                fields_truncated = True
            for node in target_nodes[: limits.max_fields_per_record]:
                value = _extract_field_value(node, field, document_base, policy)
                if value is not None:
                    values.append(value)

            if values:
                if field.many:
                    record[field.name] = tuple(values)
                else:
                    record[field.name] = values[0]
                fields_added += 1
            elif field.required:
                schema_required_missing = True

        if fields_added > 0:
            records.append(record)

    quality = Quality.MEDIUM if records else Quality.LOW

    flags: list[str] = []
    if schema_required_missing:
        flags.append("schema_required_missing")
    if records_truncated:
        flags.append("records_truncated")
    if fields_truncated:
        flags.append("fields_truncated")

    return Extraction(
        quality=quality,
        flags=flags,
        truncated=records_truncated or fields_truncated,
        records=tuple(records),
        metadata={"content_type": "text/html", "url": url} if url else {"content_type": "text/html"},
    )
