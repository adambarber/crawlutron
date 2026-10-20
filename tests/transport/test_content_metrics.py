"""content_metrics must count tags in a single pass with stable keys."""

from app.transport.body_decode import content_metrics


def test_content_metrics_counts_in_single_pass():
    html = (
        '<html><a href="/a">x</a>'
        '<iframe src="y"></iframe>'
        '<frame src="z"></frame>'
        "<frameset><frame></frame></frameset>"
        "<form><input></form></html>"
    )
    m = content_metrics("visible", html)
    assert m["link_count"] == 1
    assert m["frame_count"] == 3  # iframe + 2x <frame>
    assert m["form_count"] == 1
    assert m["has_frameset"] is True
    assert m["html_length"] == len(html)
    assert m["visible_text_length"] == len("visible")


def test_content_metrics_empty_html():
    m = content_metrics(None, None)
    assert m["link_count"] == 0
    assert m["frame_count"] == 0
    assert m["form_count"] == 0
    assert m["has_frameset"] is False
    assert m["text_to_html_ratio"] == 0.0
