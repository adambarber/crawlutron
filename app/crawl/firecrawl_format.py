"""Firecrawl-compatible Markdown and metadata extraction."""

import re
from html import unescape
from typing import Any, Optional


def extract_firecrawl_format(html: str | None, url: str) -> tuple[str | None, dict[str, Any]]:
    """Extract Firecrawl-style condensed Markdown and metadata from HTML."""
    if not html or not html.strip():
        return None, {}

    markdown_text: str | None = None
    try:
        import trafilatura
        markdown_text = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=True,
            favor_precision=True,
            url=url,
        )
    except Exception:  # noqa: BLE001  # pragma: no cover - trafilatura internal failure
        markdown_text = None

    if not markdown_text or not markdown_text.strip():
        try:
            from markdownify import markdownify as md_convert
            markdown_text = md_convert(html)  # pragma: no cover - markdownify not installed in test env
        except Exception:  # noqa: BLE001  # pragma: no cover - markdownify internal failure
            markdown_text = _text_from_html(html) or None

    return (markdown_text or "").strip() or None, _extract_firecrawl_metadata(html, url)


def _extract_firecrawl_metadata(html: str, url: str) -> dict[str, Any]:
    """Extract Firecrawl-style metadata from HTML meta tags."""
    import re
    metadata: dict[str, Any] = {"source_url": url}

    # title
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        metadata["title"] = re.sub(r"\s+", " ", title_match.group(1)).strip()

    # meta tags
    for match in re.finditer(r'<meta\s+([^>]+)>', html, flags=re.IGNORECASE):
        attrs = match.group(1)
        name_match = re.search(r'(?:name|property)\s*=\s*["\']([^"\']+)["\']', attrs, flags=re.IGNORECASE)
        content_match = re.search(r'content\s*=\s*["\']([^"\']*)["\']', attrs, flags=re.IGNORECASE)
        if not name_match or not content_match:
            continue
        name = name_match.group(1).lower().strip()
        value = content_match.group(1).strip()
        if not value:
            continue
        key_map = {
            "description": "description",
            "og:title": "og_title",
            "og:description": "og_description",
            "og:image": "og_image",
            "og:url": "og_url",
            "og:type": "og_type",
            "og:site_name": "og_site_name",
            "twitter:card": "twitter_card",
            "twitter:title": "twitter_title",
            "twitter:description": "twitter_description",
            "twitter:image": "twitter_image",
        }
        mapped = key_map.get(name)
        if mapped:
            metadata[mapped] = value

    # canonical URL
    canonical_match = re.search(r'<link\s+[^>]*rel\s*=\s*["\']canonical["\'][^>]*href\s*=\s*["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    if not canonical_match:
        canonical_match = re.search(r'<link\s+[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*rel\s*=\s*["\']canonical["\']', html, flags=re.IGNORECASE)
    if canonical_match:
        metadata["canonical_url"] = canonical_match.group(1).strip()

    # favicon
    favicon_match = re.search(r'<link\s+[^>]*rel\s*=\s*["\'](?:icon|shortcut icon|apple-touch-icon)["\'][^>]*href\s*=\s*["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    if not favicon_match:
        favicon_match = re.search(r'<link\s+[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*rel\s*=\s*["\'](?:icon|shortcut icon|apple-touch-icon)["\']', html, flags=re.IGNORECASE)
    if favicon_match:
        metadata["favicon"] = favicon_match.group(1).strip()

    # language
    lang_match = re.search(r'<html\s+[^>]*lang\s*=\s*["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    if lang_match:
        metadata["language"] = lang_match.group(1).strip()

    return metadata

def _text_from_html(html: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    without_styles = re.sub(r"<style\b[^>]*>.*?</style>", " ", without_scripts, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", without_styles)
    return re.sub(r"\s+", " ", unescape(text)).strip()

