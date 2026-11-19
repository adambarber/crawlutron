"""HTML diagnostics: bounded counts and records over one parse."""

from __future__ import annotations

from crawlutron_extract import html_diagnostics


def test_empty_html_is_zero_counts():
    assert html_diagnostics("") == {
        "frame_count": 0,
        "form_count": 0,
        "link_count": 0,
        "has_frameset": False,
        "frames": [],
        "forms": [],
    }


def test_frameset_page_marks_has_frameset():
    html = "<frameset><frame src='f1'><frame src='f2'></frameset>"
    out = html_diagnostics(html)
    assert out["has_frameset"] is True
    assert out["frame_count"] == 2
    assert out["frames"][0]["src"] == "f1"


def test_nested_iframes_counted():
    html = "<iframe src='a'><iframe src='b'></iframe>"
    out = html_diagnostics(html)
    assert out["frame_count"] == 2


def test_form_with_fields():
    html = (
        "<form action='/submit' method='post'>"
        "<input name='q' value='v'><textarea name='t'>text</textarea>"
        "</form>"
    )
    out = html_diagnostics(html)
    assert out["form_count"] == 1
    assert out["forms"][0]["action"] == "/submit"
    assert out["forms"][0]["method"] == "POST"
    names = [f["name"] for f in out["forms"][0]["fields"]]
    assert "q" in names and "t" in names


def test_links_counted_and_deduped():
    html = (
        '<a href="http://example.com/one">One</a>'
        '<a href="http://example.com/one">One again</a>'
        '<a href="http://example.com/two">Two</a>'
    )
    out = html_diagnostics(html)
    assert out["link_count"] == 2


def test_bytes_input_accepted():
    assert html_diagnostics(b'<a href="http://example.com/x">x</a>')["link_count"] == 1


def test_relative_links_refused_without_source_url():
    assert html_diagnostics('<a href="/one">One</a>')["link_count"] == 0
