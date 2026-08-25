"""Single-pass haystack construction: same results, fewer string copies."""


def _strip(html):
    from app.classify import _strip_html_noise
    return _strip_html_noise(html)


def test_strips_scripts_styles_comments_in_one_pass():
    html = (
        "<html><head><script>var a=1;</script>"
        "<style>.x{}</style><!-- comment --><body><p>visible text</p></body></html>"
    )
    out = _strip(html)
    assert "var a=1" not in out
    assert ".x{}" not in out
    assert "comment" not in out
    assert "visible text" in out


def test_case_insensitive_tags():
    out = _strip("<SCRIPT>x()</SCRIPT><p>keep me</p>")
    assert "x()" not in out and "keep me" in out


def test_classify_unchanged_after_refactor():
    from app.classify import classify_page

    # spoof attempt still neutralized
    assert classify_page(
        "<!-- access denied --><body>Welcome to our site</body>", "Welcome to our site"
    ) == "rendered"
    # visible block phrase (hard classification) detected
    assert classify_page("<body>access denied</body>", "access denied") == "access_denied"
