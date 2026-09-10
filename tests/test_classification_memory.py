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
    assert (
        classify_page("<!-- access denied --><body>Welcome to our site</body>", "Welcome to our site")
        == "rendered"
    )
    # visible block phrase (hard classification) detected
    assert classify_page("<body>access denied</body>", "access denied") == "access_denied"


def test_strip_is_linear_under_pathological_unclosed_script_flood():
    """Unclosed <script> tags must not trigger quadratic regex backtracking.

    A page with N unclosed <script> tags made _HTML_NOISE_RE cost O(N^2):
    each tag anchor re-scanned to end-of-string for a closing tag that never
    comes (adversarial review H-final-1). The replacement scanner must stay
    linear: doubling the input must roughly double, not quadruple, the time.
    """
    import time

    from app.classify import _strip_html_noise

    def timed(n):
        payload = ("<script>" + "a" * 500) * n
        # Warm-up once to settle first-call effects; min-of-3 so timer
        # noise on a loaded runner can't flake a linear implementation
        # (final review #6).
        _strip_html_noise(payload)
        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            _strip_html_noise(payload)
            elapsed = time.perf_counter() - t0
            best = elapsed if best is None else min(best, elapsed)
        return best

    t_small = timed(400)
    t_big = timed(800)
    assert t_big < t_small * 3.5, (
        f"super-linear blowup: {t_small:.3f}s -> {t_big:.3f}s (ratio {t_big / t_small:.1f}x for 2x input)"
    )
    # Absolute ceiling: the ~400 KB pass must complete well under one CI
    # tick on any reasonable machine; a quadratic reimplementation would
    # be orders of magnitude slower.
    assert t_big < 0.25, f"linear scan too slow: {t_big:.3f}s for ~400 KB"


def test_strip_unclosed_script_neutralizes_to_end_and_keeps_preceding_text():
    """An unclosed <script> swallows the remainder (browser-consistent)."""
    out = _strip("<p>visible</p><script>alert(1);<p>hidden tail</p>")
    assert "visible" in out
    assert "alert(1)" not in out
    assert "hidden tail" not in out


def test_strip_comment_and_style_parities():
    # Unclosed comment swallows the remainder
    out = _strip("<p>keep</p><!-- secret <p>gone</p>")
    assert "keep" in out and "secret" not in out and "gone" not in out
    # Unclosed style swallows the remainder
    out2 = _strip("<p>keep2</p><style>.a{}<p>gone2</p>")
    assert "keep2" in out2 and ".a{}" not in out2 and "gone2" not in out2
    # Adjacent blocks without whitespace survive intact
    out3 = _strip("<script>a</script><style>b</style><!--c--><p>tail</p>")
    assert "tail" in out3 and "a" not in out3.split("tail")[0].replace("<script>a</script>", "")
