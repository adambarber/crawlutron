"""HTML forms and frames parity: required, textarea, select options, frameset, srcdoc cap, sandbox."""

from __future__ import annotations

from crawlutron_extract import Quality, extract


def _field_from_html(html: bytes) -> dict:
    result = extract(html, "text/html")
    return result.forms[0]["fields"][0]


def test_form_field_required_present():
    """required attribute is carried on the field."""
    field = _field_from_html(b'<form><input name="q" required></form>')
    assert field["name"] == "q"
    assert field["required"] == "true"


def test_form_field_required_absent():
    """A field without required records required=false."""
    field = _field_from_html(b'<form><input name="q"></form>')
    assert field["name"] == "q"
    assert field["required"] == "false"


def test_form_fields_without_form_wrapper():
    """Form controls outside a <form> still yield fields."""
    result = extract(b'<input name="q" required>', "text/html")
    assert len(result.forms) == 1
    field = result.forms[0]["fields"][0]
    assert field["name"] == "q"
    assert field["required"] == "true"


def test_textarea_content_is_value():
    """textarea element body becomes the field value."""
    result = extract(b'<textarea name="msg">hello world</textarea>', "text/html")
    field = result.forms[0]["fields"][0]
    assert field["name"] == "msg"
    assert field["value"] == "hello world"


def test_select_options_each_become_fields():
    """Every <option> becomes a field carrying the select name and value."""
    html = b'<select name="s"><option value="a">A</option><option value="b">B</option></select>'
    result = extract(html, "text/html")
    values = {f["value"] for f in result.forms[0]["fields"]}
    assert values == {"a", "b"}
    assert all(f["name"] == "s" for f in result.forms[0]["fields"])


def test_select_selected_option_marked():
    """The selected option is marked."""
    html = b'<select name="s"><option value="a">A</option><option value="b" selected>B</option></select>'
    result = extract(html, "text/html")
    by_value = {f["value"]: f for f in result.forms[0]["fields"]}
    assert by_value["b"]["selected"] == "true"
    assert by_value["a"]["selected"] == "false"


def test_frameset_frame_collected():
    """Legacy frameset/frame elements are collected like iframes."""
    html = b'<frameset><frame src="https://example.com/f1" name="f1"/></frameset>'
    result = extract(html, "text/html")
    assert len(result.frames) == 1
    assert result.frames[0]["src"] == "https://example.com/f1"
    assert result.frames[0]["name"] == "f1"


def test_iframe_sandbox_stored():
    """sandbox attribute is stored on the frame entry."""
    html = b'<iframe sandbox="allow-scripts" src="https://example.com/x"></iframe>'
    result = extract(html, "text/html")
    assert result.frames[0]["sandbox"] == "allow-scripts"


def test_iframe_srcdoc_stored():
    """iframe srcdoc attribute is recorded."""
    html = b'<iframe srcdoc="<p>inline</p>"></iframe>'
    result = extract(html, "text/html")
    assert result.frames[0]["srcdoc"] == "<p>inline</p>"


def test_srcdoc_recursion_within_cap():
    """srcdoc content nested within the depth cap is collected."""
    inner = b"<iframe srcdoc='<p>nested</p>'></iframe>"
    html = b'<iframe srcdoc="' + inner + b'"></iframe>'
    result = extract(html, "text/html")
    assert "nested" in result.text


def test_srcdoc_recursion_beyond_cap_refused():
    """srcdoc nested beyond the depth cap is refused with a flag, never crashes."""

    def nest(levels: int) -> bytes:
        # Recursively entity-encode so each level's srcdoc attribute decodes to
        # the next level's iframe markup when the outer parser reads it.
        inner = "<p>deep</p>"
        for _ in range(levels):
            escaped = (
                inner.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;")
            )
            inner = '<iframe srcdoc="' + escaped + '"></iframe>'
        return inner.encode("ascii")

    result = extract(nest(6), "text/html")
    assert "srcdoc_depth_refused" in result.flags
    assert "deep" not in result.text


def test_quality_medium_with_form():
    """A page with only a form still yields MEDIUM if body text present."""
    html = b'<p>body</p><form><input name="q"></form>'
    result = extract(html, "text/html")
    assert result.quality == Quality.MEDIUM
