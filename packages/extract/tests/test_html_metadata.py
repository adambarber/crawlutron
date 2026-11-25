"""HTML metadata parity: JSON-LD, og/twitter, favicon, canonical, lang, hreflang, base."""

from __future__ import annotations

from crawlutron_extract import Quality, extract


def test_json_ld_single_extracted():
    """One application/ld+json block is captured as json_ld."""
    html = b'<script type="application/ld+json">{"@type":"Article","headline":"Hi"}</script>'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["json_ld"] == '{"@type":"Article","headline":"Hi"}'


def test_json_ld_multiple_joined():
    """Multiple JSON-LD blocks are joined deterministically."""
    html = (
        b'<script type="application/ld+json">{"@type":"Article"}</script>'
        b'<script type="application/ld+json">{"@type":"Organization"}</script>'
    )
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["json_ld"] == '{"@type":"Article"}\n{"@type":"Organization"}'


def test_json_ld_malformed_skipped():
    """Malformed JSON-LD is skipped, never raises."""
    html = b'<script type="application/ld+json">not json</script>'
    result = extract(html, "text/html", url="https://example.com/")
    assert "json_ld" not in result.metadata


def test_json_ld_scalar_root_skipped():
    """A JSON-LD block whose root is not dict/list is skipped (shape enforced)."""
    html = b'<script type="application/ld+json">"just a string"</script>'
    result = extract(html, "text/html", url="https://example.com/")
    assert "json_ld" not in result.metadata


def test_json_ld_wrong_type_skipped():
    """A script without the ld+json type is not JSON-LD."""
    html = b'<script type="application/javascript">{"x":1}</script>'
    result = extract(html, "text/html", url="https://example.com/")
    assert "json_ld" not in result.metadata


def test_twitter_card_mapped():
    """twitter:* meta tags map to core PageMetadata names."""
    html = (
        b'<meta name="twitter:card" content="summary">'
        b'<meta name="twitter:image" content="https://example.com/img.jpg">'
    )
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["twitter_card"] == "summary"
    assert result.metadata["twitter_image"] == "https://example.com/img.jpg"


def test_og_image_mapped():
    """og:image maps to core PageMetadata name."""
    html = b'<meta property="og:image" content="https://example.com/og.jpg">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["og_image"] == "https://example.com/og.jpg"


def test_favicon_from_link_icon():
    """link rel=icon yields favicon, resolved against document URL."""
    html = b'<link rel="icon" href="/favicon.ico">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["favicon"] == "https://example.com/favicon.ico"


def test_favicon_from_link_shortcut():
    """link rel=shortcut icon yields favicon."""
    html = b'<link rel="shortcut icon" href="https://cdn.example.com/f.ico">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["favicon"] == "https://cdn.example.com/f.ico"


def test_canonical_from_link():
    """link rel=canonical yields canonical_url, resolved against document URL."""
    html = b'<link rel="canonical" href="/page">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["canonical_url"] == "https://example.com/page"


def test_canonical_first_wins():
    """Two canonical links: the first is kept (core precedence)."""
    html = b'<link rel="canonical" href="/first"><link rel="canonical" href="/second">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["canonical_url"] == "https://example.com/first"


def test_language_html_lang_precedence():
    """<html lang> wins over <meta name=lang>."""
    html = b'<html lang="fr"><meta name="lang" content="en"></html>'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["language"] == "fr"


def test_language_from_meta_lang_fallback():
    """<meta name=lang> is used when there is no <html lang>."""
    html = b'<meta name="lang" content="en">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["language"] == "en"


def test_hreflang_stored():
    """link hreflang is captured."""
    html = b'<link rel="alternate" hreflang="es" href="/es/">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["hreflang"] == "es"


def test_base_resolves_canonical():
    """<base href> changes the resolution base for canonical URLs."""
    html = b'<base href="https://other.com/"><link rel="canonical" href="/page">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["canonical_url"] == "https://other.com/page"


def test_base_resolves_favicon():
    """<base href> changes the resolution base for favicon URLs."""
    html = b'<base href="https://other.com/"><link rel="icon" href="f.ico">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["favicon"] == "https://other.com/f.ico"


def test_base_resolves_links():
    """<base href> changes the resolution base for admitted links."""
    html = b'<base href="https://other.com/"><a href="/page">Link</a>'
    result = extract(html, "text/html", url="https://example.com/")
    assert len(result.links) == 1
    link = result.links[0]
    assert str(link) == "https://other.com/page"


def test_description_and_title_from_meta():
    """<meta name=description> and <title> are captured."""
    html = b'<title>Page Title</title><meta name="description" content="A page">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.metadata["title"] == "Page Title"
    assert result.metadata["description"] == "A page"


def test_quality_medium_with_metadata():
    """Metadata-only pages still reach MEDIUM when body text present."""
    html = b'<p>body text</p><meta name="description" content="A page">'
    result = extract(html, "text/html", url="https://example.com/")
    assert result.quality == Quality.MEDIUM
