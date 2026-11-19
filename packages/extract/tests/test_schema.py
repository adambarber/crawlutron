"""Schema-driven field extraction from HTML (Slice 10).

CSS-selector based fields over a bounded DOM, with record containers.
"""

from __future__ import annotations

from crawlutron_extract import ExtractLimits, Quality, extract_records
from crawlutron_extract.models import Extraction
from crawlutron_extract.schema import FieldKind, FieldSpec, Schema


def _f(name: str, selector: str, kind: FieldKind = FieldKind.TEXT, **kwargs) -> FieldSpec:
    return FieldSpec(name=name, selector=selector, kind=kind, **kwargs)


def _s(record: str | None, *fields: FieldSpec) -> Schema:
    return Schema(record=record, fields=fields)


def _extract_schema(html: str | bytes, record: str | None, *fields: FieldSpec, **kwargs) -> Extraction:
    return extract_records(html, _s(record, *fields), **kwargs)


def test_schema_basic_text():
    """Basic TEXT field extraction from a single record."""
    html = b"<div class='record'><span class='title'>Hello</span></div>"
    result = _extract_schema(html, ".record", _f("title", ".title"))
    assert result.records == ({"title": "Hello"},)
    assert result.quality == Quality.MEDIUM


def test_schema_multiple_fields():
    """Multiple fields extracted from a single record."""
    html = b"<div class='record'><span class='title'>Hello</span><span class='desc'>World</span></div>"
    result = _extract_schema(html, ".record", _f("title", ".title"), _f("desc", ".desc"))
    assert result.records == ({"title": "Hello", "desc": "World"},)


def test_schema_multiple_records():
    """Multiple record containers each produce a record."""
    html = b"""
    <div class='record'><span class='title'>First</span></div>
    <div class='record'><span class='title'>Second</span></div>
    """
    result = _extract_schema(html, ".record", _f("title", ".title"))
    assert result.records == ({"title": "First"}, {"title": "Second"})


def test_schema_many_fields():
    """many=True collects every match."""
    html = b"<div class='record'><li class='item'>A</li><li class='item'>B</li><li class='item'>C</li></div>"
    result = _extract_schema(html, ".record", _f("items", ".item", many=True))
    assert result.records == ({"items": ("A", "B", "C")},)


def test_schema_required_field_missing():
    """Required field missing → schema_required_missing flag."""
    html = b"<div class='record'><span class='title'>Hello</span></div>"
    result = _extract_schema(html, ".record", _f("title", ".title"), _f("desc", ".desc", required=True))
    assert result.records == ({"title": "Hello"},)
    assert "schema_required_missing" in result.flags


def test_schema_number_coercion():
    """NUMBER kind coerces text to float."""
    html = b"<div class='record'><span class='price'>42.5</span></div>"
    result = _extract_schema(html, ".record", _f("price", ".price", kind=FieldKind.NUMBER))
    assert result.records == ({"price": 42.5},)


def test_schema_integer_coercion():
    """INTEGER kind coerces text to int."""
    html = b"<div class='record'><span class='count'>42</span></div>"
    result = _extract_schema(html, ".record", _f("count", ".count", kind=FieldKind.INTEGER))
    assert result.records == ({"count": 42},)


def test_schema_number_unparseable_required():
    """NUMBER field with unparseable text and required=True → schema_required_missing."""
    html = b"<div class='record'><span class='price'>not a number</span></div>"
    result = _extract_schema(html, ".record", _f("price", ".price", kind=FieldKind.NUMBER, required=True))
    assert result.records == ()
    assert "schema_required_missing" in result.flags


def test_schema_link_admission():
    """LINK kind admits through safe_url."""
    html = b'<div class="record"><a class="link" href="/page">Link</a></div>'
    result = _extract_schema(
        html,
        ".record",
        _f("link", ".link", kind=FieldKind.LINK, attribute="href"),
        url="https://example.com/",
    )
    assert result.records == ({"link": "https://example.com/page"},)


def test_schema_link_refused():
    """LINK kind refuses unsafe URLs."""
    html = b'<div class="record"><a class="link" href="javascript:alert(1)">Link</a></div>'
    result = _extract_schema(html, ".record", _f("link", ".link", kind=FieldKind.LINK, attribute="href"))
    assert result.records == ()


def test_schema_attr_extraction():
    """ATTR kind extracts an attribute value."""
    html = b'<div class="record"><img class="img" src="image.png" alt="Image"></div>'
    result = _extract_schema(html, ".record", _f("src", ".img", kind=FieldKind.ATTR, attribute="src"))
    assert result.records == ({"src": "image.png"},)


def test_schema_invalid_selector():
    """Unsupported selector → selector_invalid flag and no records."""
    html = b"<div class='record'><span class='title'>Hello</span></div>"
    result = _extract_schema(html, ".record", _f("title", ".title::unknown"))
    assert result.records == ()
    assert "selector_invalid" in result.flags


def test_schema_no_records():
    """Selector matching nothing → empty records, quality=LOW."""
    html = b"<div class='record'><span class='title'>Hello</span></div>"
    result = _extract_schema(html, ".nonexistent", _f("title", ".title"))
    assert result.records == ()
    assert result.quality == Quality.LOW


def test_schema_no_record_selector():
    """record=None means whole document is one record."""
    html = b"<div><span class='title'>Hello</span></div>"
    result = _extract_schema(html, None, _f("title", ".title"))
    assert result.records == ({"title": "Hello"},)


def test_schema_determinism():
    """Deterministic output."""
    html = b"<div class='record'><span class='title'>Hello</span></div>"
    limits = ExtractLimits()
    result = _extract_schema(html, ".record", _f("title", ".title"), limits=limits)
    result2 = _extract_schema(html, ".record", _f("title", ".title"), limits=limits)
    assert result == result2


def test_schema_child_combinator():
    """Child combinator (>) selects direct children."""
    html = b"<div class='record'><span class='title'><span class='inner'>Nested</span></span></div>"
    result = _extract_schema(html, ".record", _f("title", ".title > span"))
    assert result.records == ({"title": "Nested"},)


def test_schema_adjacent_combinator():
    """Adjacent combinator (+) selects the next sibling."""
    html = b"<div class='record'><span class='first'>First</span><span class='second'>Second</span></div>"
    result = _extract_schema(html, ".record", _f("second", ".first + span"))
    assert result.records == ({"second": "Second"},)


def test_schema_comma_list():
    """Comma-separated selector list matches any."""
    html = b"<div class='record'><span class='title'>Hello</span></div>"
    result = _extract_schema(html, ".record", _f("title", ".title, .heading"))
    assert result.records == ({"title": "Hello"},)


def test_schema_first_child():
    """:first-child matches the first element child."""
    html = b"<div class='record'><span class='first'>First</span><span class='second'>Second</span></div>"
    result = _extract_schema(html, ".record", _f("first", "span:first-child"))
    assert result.records == ({"first": "First"},)


def test_schema_last_child():
    """:last-child matches the last element child."""
    html = b"<div class='record'><span class='first'>First</span><span class='second'>Second</span></div>"
    result = _extract_schema(html, ".record", _f("last", "span:last-child"))
    assert result.records == ({"last": "Second"},)


def test_schema_nth_child():
    """:nth-child(n) matches the nth element child."""
    html = (
        b"<div class='record'><span class='a'>A</span><span class='b'>B</span><span class='c'>C</span></div>"
    )
    result = _extract_schema(html, ".record", _f("second", "span:nth-child(2)"))
    assert result.records == ({"second": "B"},)


def test_schema_not_selector():
    """:not(simple) excludes matching elements."""
    html = b"<div class='record'><span class='keep'>Keep</span><span class='drop'>Drop</span></div>"
    result = _extract_schema(html, ".record", _f("kept", "span:not(.drop)", many=True))
    assert result.records == ({"kept": ("Keep",)},)


def test_schema_records_truncated():
    """max_records cap truncates records."""
    html = "".join(f"<div class='record'><span class='title'>Item {i}</span></div>" for i in range(5))
    limits = ExtractLimits(max_records=2)
    result = _extract_schema(html, ".record", _f("title", ".title"), limits=limits)
    assert len(result.records) == 2
    assert "records_truncated" in result.flags
    assert result.truncated is True


def test_schema_fields_truncated():
    """max_fields_per_record cap truncates fields."""
    html = (
        b"<div class='record'><span class='f1'>1</span>"
        b"<span class='f2'>2</span><span class='f3'>3</span></div>"
    )
    limits = ExtractLimits(max_fields_per_record=2)
    result = _extract_schema(
        html,
        ".record",
        _f("f1", ".f1"),
        _f("f2", ".f2"),
        _f("f3", ".f3"),
        limits=limits,
    )
    assert len(result.records[0]) == 2
    assert "fields_truncated" in result.flags
    assert result.truncated is True


def test_schema_quality_failed_no_records():
    """No records → quality=LOW."""
    html = b"<div class='record'><span class='title'>Hello</span></div>"
    result = _extract_schema(html, ".nonexistent", _f("title", ".title"))
    assert result.quality == Quality.LOW


def test_schema_markdown_renders():
    """MARKDOWN routes through the markdown emitter (emphasis, list, link content)."""
    html = b"<p><em>x</em> <ul><li>a</li><li>b</li></ul> <a href=/u>link</a></p>"
    result = _extract_schema(html, None, _f("m", "p", kind=FieldKind.MARKDOWN))
    assert len(result.records) == 1
    md = result.records[0]["m"]
    assert isinstance(md, str)
    assert "*x*" in md
    assert "- a" in md
    assert "- b" in md
    assert "link" in md


def test_schema_link_honours_base_href():
    """LINK resolves against a document <base href>, not just the url argument."""
    html = b'<base href="https://example.com/base/"><a href="page">L</a>'
    result = _extract_schema(html, None, _f("l", "a", kind=FieldKind.LINK), url="https://example.com/")
    assert result.records == ({"l": "https://example.com/base/page"},)


def test_schema_duplicate_field_names_refused():
    """Duplicate field names are refused at Schema construction, naming the field."""
    try:
        Schema(record=None, fields=(_f("x", ".a"), _f("x", ".b")))
    except ValueError as exc:
        assert "x" in str(exc)
    else:
        raise AssertionError("duplicate field names were not refused")


def test_schema_empty_document_no_records():
    """record=None with no matching field yields no records and never raises quality."""
    html = b"<div>nothing here</div>"
    result = _extract_schema(html, None, _f("t", ".missing"))
    assert result.records == ()
    assert result.quality == Quality.LOW
