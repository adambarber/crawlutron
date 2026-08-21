"""Unified content extraction pipeline + discovered-link/frame/form diagnostics.

Every access strategy (HTTP, browser, document/PDF, Trafilatura) funnels its
raw fetched/rendered bytes through :func:`extract_content`, which returns a
consistent ``(text, markdown)`` pair regardless of content type:

* HTML web pages  -> trafilatura (boilerplate removed, main content kept)
* Documents       -> markitdown (PDF/DOCX/PPTX/XLSX/CSV/JSON/XML/EPub -> Markdown)
* Plain text      -> passed through as Markdown

This keeps the agent-facing output uniform (always Markdown) and token-minded:
web pages lose nav/footer/ads boilerplate, while documents keep their structure.

This module also owns the structured discovery diagnostics (links, frames,
forms), Katana-inspired JS endpoint discovery, and auto-pagination detection.
"""

from __future__ import annotations

import concurrent.futures
import io
import logging
import os
import re
from html import unescape
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

from app.crawl.firecrawl_format import _text_from_html
from app.models import DiscoveredLink
from app.validation import normalize_public_http_url

logger = logging.getLogger(__name__)

# Wall-clock bound (seconds) for a single CSS/XPath selector evaluation.
# A pathological selector (or adversarial payload) could otherwise run
# unbounded and tie up a request worker. Override via env var.
_EXTRACTION_SELECTOR_TIMEOUT = float(
    os.environ.get("BROWSER_CRAWLER_EXTRACTION_SELECTOR_TIMEOUT", "2.0")
)

_EXTRACTION_SELECTOR_SENTINEL = object()


def _eval_selector(fn, timeout):
    """Run a selector evaluation in a worker thread so it cannot hang a request.

    Returns the callable's result, or ``_EXTRACTION_SELECTOR_SENTINEL`` if it
    exceeded ``timeout`` seconds. The runaway worker is abandoned (not joined)
    on timeout, which bounds caller latency even though the evaluation keeps
    running briefly in the background.

    This is the defense for the XPath/CSS DoS finding: user-supplied selectors
    are evaluated with a hard wall-clock cap.
    """
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return _EXTRACTION_SELECTOR_SENTINEL
    finally:
        ex.shutdown(wait=False)


_PDF_MAGIC = b"%PDF"
_DOC_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".json", ".xml", ".epub",
)


class ExtractedContent(NamedTuple):
    text: str | None
    markdown: str | None


def _decode(body: bytes | str) -> str:
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body


def _is_html(content_type: str | None, body: bytes | str) -> bool:
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    text = _decode(body).lstrip().lower()
    return text.startswith("<html") or text.startswith("<!doctype html>")


def _looks_like_document(content_type: str | None, body: bytes | str, url: str | None) -> bool:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return True
    if "html" in ct:
        return False
    lower_url = (url or "").split("?")[0].lower()
    if any(lower_url.endswith(ext) for ext in _DOC_EXTENSIONS):
        return True
    if isinstance(body, bytes) and body.startswith(_PDF_MAGIC):
        return True
    if ct in ("application/json", "text/csv", "application/xml", "text/xml", "application/epub+zip"):
        return True
    return False


def extract_content(
    content: bytes | str,
    content_type: str | None,
    url: str | None,
    css_selector: str | None = None,
    xpath: str | None = None,
) -> ExtractedContent:
    """Extract ``(text, markdown)`` from raw content using the unified pipeline."""
    if _is_html(content_type, content) and (css_selector or xpath):
        return _extract_selected(_decode(content), url, css_selector=css_selector, xpath=xpath)
    if _is_html(content_type, content):
        return _extract_html(_decode(content), url)
    if _looks_like_document(content_type, content, url):
        body = content if isinstance(content, bytes) else content.encode("utf-8", "replace")
        return _extract_document(body, url)
    text = _decode(content).strip()
    return ExtractedContent(text or None, text or None)


def _html_to_markdown(html_fragment: str) -> tuple[str | None, str | None]:
    """Convert a raw HTML fragment to ``(text, markdown)`` for DOM targeting."""
    try:
        from markdownify import markdownify as html_to_md

        md = html_to_md(html_fragment) or None
    except Exception:  # noqa: BLE001
        md = None
    text = _text_from_html(html_fragment) or None
    return text, md


def _extract_selected(
    html: str,
    source_url: str | None,
    *,
    css_selector: str | None = None,
    xpath: str | None = None,
) -> ExtractedContent:
    """Mechanical DOM targeting: extract only the matched nodes as Markdown.

    CSS selectors are resolved with BeautifulSoup; XPath expressions with lxml.
    The matched subtree(s) are serialized and converted to Markdown, so callers
    can target a specific section (e.g. an agenda table) without Trafilatura's
    full-page boilerplate removal.
    """
    if not html:
        return ExtractedContent(None, None)
    fragment: str | None = None
    if css_selector:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return ExtractedContent(None, None)
        soup = BeautifulSoup(html, "html.parser")
        selected = _eval_selector(
            lambda: soup.select(css_selector), _EXTRACTION_SELECTOR_TIMEOUT
        )
        if selected is _EXTRACTION_SELECTOR_SENTINEL:
            logger.warning(
                "extraction_selector_timeout",
                extra={"selector_type": "css", "selector": css_selector},
            )
            return ExtractedContent(None, None)
        if not selected:
            return ExtractedContent(None, None)
        fragment = "\n".join(str(tag) for tag in selected)
    elif xpath:
        tree = _parse_html_tree(html)
        if tree is None:
            return ExtractedContent(None, None)
        try:
            nodes = _eval_selector(
                lambda: tree.xpath(xpath), _EXTRACTION_SELECTOR_TIMEOUT
            )
        except Exception:  # invalid XPath is caller error, not extraction
            return ExtractedContent(None, None)
        if nodes is _EXTRACTION_SELECTOR_SENTINEL:
            logger.warning(
                "extraction_selector_timeout",
                extra={"selector_type": "xpath", "selector": xpath},
            )
            return ExtractedContent(None, None)
        if not nodes:
            return ExtractedContent(None, None)
        from lxml import etree

        parts: list[str] = []
        for node in nodes:
            if isinstance(node, str):
                parts.append(node)
            else:
                parts.append(etree.tostring(node, encoding="unicode"))
        fragment = "\n".join(parts)
    if not fragment:
        return ExtractedContent(None, None)
    text, md = _html_to_markdown(fragment)
    return ExtractedContent(md or text, md or text)


def _extract_html(
    html: str,
    url: str | None,
    css_selector: str | None = None,
    xpath: str | None = None,
) -> ExtractedContent:
    if css_selector or xpath:
        return _extract_selected(html, url, css_selector=css_selector, xpath=xpath)
    try:
        import trafilatura
        md = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            favor_recall=True,
            include_comments=False,
            include_tables=True,
            include_formatting=True,
            include_links=True,
            deduplicate=True,
        )
    except Exception:  # noqa: BLE001
        md = None
    if not md or not md.strip():
        # Fallback to markdownify before the plain-text extractor so that
        # pages Trafilatura refuses still become usable Markdown.
        try:
            from markdownify import markdownify as html_to_md

            md = html_to_md(html) or None
        except Exception:  # noqa: BLE001
            md = None
    if not md or not md.strip():
        md = _text_from_html(html) or None
    return ExtractedContent(md, md)


def _extract_document(body: bytes, url: str | None) -> ExtractedContent:
    try:
        from markitdown import MarkItDown
    except ImportError:
        return ExtractedContent(None, None)
    try:
        res = MarkItDown().convert_stream(io.BytesIO(body))
        md = res.text_content if res is not None else None
    except Exception:  # noqa: BLE001
        md = None
    if not md or not md.strip():
        return ExtractedContent(None, None)
    return ExtractedContent(md, md)


# ---------------------------------------------------------------------------
# Structured discovery diagnostics: links, frames, forms
# ---------------------------------------------------------------------------


def _parse_html_tree(html: str | None):
    """Parse HTML once with lxml and return the root element.

    All discovery passes (anchors, frames, forms) and the XPath selector path
    reuse this single parse so we allocate and walk the tree only once instead
    of running three separate :class:`HTMLParser` subclasses.

    ``lxml.etree.HTML`` (rather than ``lxml.html.fromstring``) is used because
    it wraps fragments in a document tree and preserves legacy ``<frameset>`` /
    ``<frame>`` markup that the lenient ``fromstring`` path otherwise drops.
    Returns ``None`` for empty/invalid input.
    """
    if not html:
        return None
    try:
        from lxml import etree
        from lxml.etree import ParserError
    except ImportError:
        return None
    try:
        return etree.HTML(html)
    except (ParserError, ValueError, TypeError):
        return None


class _FrameCollector:
    """Back-compat collector: parse once with lxml and collect frame/iframe srcs.

    Kept because :mod:`app.service` and the test-suite build it directly via
    ``feed()``; internally it now shares the single lxml parse.
    """

    def __init__(self) -> None:
        self.frames: list[dict[str, str]] = []

    def feed(self, html: str) -> None:
        tree = _parse_html_tree(html)
        if tree is None:
            return
        for el in tree.iter("frame", "iframe"):
            src = (el.get("src") or "").strip()
            if not src:
                continue
            self.frames.append(
                {
                    "url": src,
                    "name": el.get("name") or "",
                    "title": el.get("title") or "",
                }
            )


def _extract_frames(html: str | None, source_url: str | None) -> list[dict[str, Any]]:
    if not html or not source_url:
        return []
    source_host = (urlparse(source_url).hostname or "").lower()
    tree = _parse_html_tree(html)
    if tree is None:
        return []
    frames: list[dict[str, Any]] = []
    seen: set[str] = set()
    for el in tree.iter("frame", "iframe"):
        src = (el.get("src") or "").strip()
        if not src:
            continue
        normalized_url = urljoin(source_url, src)
        parsed = urlparse(normalized_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        frames.append(
            {
                "url": src,
                "normalized_url": normalized_url,
                "name": el.get("name") or "",
                "title": el.get("title") or "",
                "source_url": source_url,
                "host": parsed.hostname.lower(),
                "is_same_host": parsed.hostname.lower() == source_host,
            }
        )
    return frames


_CONTROL_TAGS = ("input", "select", "textarea", "button")


def _extract_forms(html: str | None, source_url: str | None) -> list[dict[str, Any]]:
    if not html or not source_url:
        return []
    tree = _parse_html_tree(html)
    if tree is None:
        return []
    forms: list[dict[str, Any]] = []
    for form_el in tree.iter("form"):
        attrs = {key.lower(): value for key, value in form_el.attrib.items()}
        action = unescape(str(attrs.get("action", "")).strip())
        normalized_action = urljoin(source_url, action) if action else source_url
        controls: list[dict[str, str]] = []
        for control in form_el.iter(*_CONTROL_TAGS):
            cattrs = {key.lower(): value for key, value in control.attrib.items()}
            tag = str(control.tag)
            control_type = (cattrs.get("type") or "text").lower() if tag == "input" else cattrs.get("type", "")
            controls.append(
                {
                    "tag": tag,
                    "type": control_type,
                    "name": cattrs.get("name", ""),
                    "id": cattrs.get("id", ""),
                    "value": cattrs.get("value", ""),
                }
            )
        forms.append(
            {
                "method": (attrs.get("method") or "GET").upper(),
                "action": action,
                "normalized_action": normalized_action,
                "target": attrs.get("target", ""),
                "name": attrs.get("name", ""),
                "id": attrs.get("id", ""),
                "controls": controls,
            }
        )
    return forms


def _html_diagnostics(html: str | None, source_url: str | None) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    frames = _extract_frames(html, source_url)
    forms = _extract_forms(html, source_url)
    if frames:
        diagnostics["frames"] = frames
    if forms:
        diagnostics["forms"] = forms
    return diagnostics




def _extract_discovered_links(
    html: str | None, source_url: str | None, *, max_links: int = 250
) -> tuple[list[Any], int, bool]:
    if not html or not source_url:
        return [], 0, False
    try:
        norm_source = normalize_public_http_url(source_url)
    except ValueError:
        norm_source = source_url
    source_host = (urlparse(norm_source).hostname or "").lower()
    tree = _parse_html_tree(html)
    if tree is None:
        return [], 0, False
    anchors: list[dict[str, Any]] = []
    for el in tree.iter("a"):
        # Skip anchors nested inside another anchor to avoid double counting.
        parent = el.getparent()
        if parent is not None and str(parent.tag) == "a":
            continue
        attrs = {key.lower(): value for key, value in el.attrib.items()}
        text = re.sub(r"\s+", " ", unescape("".join(el.itertext()))).strip()
        anchors.append({"attrs": attrs, "text": [text]})
    links: list[Any] = []
    seen: set[str] = set()
    total_count = 0
    for anchor in anchors:
        attrs = anchor["attrs"]
        href = unescape(str(attrs.get("href", "")).strip())
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            normalized_url = normalize_public_http_url(urljoin(source_url, href))
        except ValueError:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        total_count += 1
        if len(links) >= max_links:
            continue
        parsed = urlparse(normalized_url)
        text = re.sub(r"\s+", " ", unescape(" ".join(anchor["text"]))).strip()
        links.append(
            DiscoveredLink(
                url=href,
                normalized_url=normalized_url,
                text=text,
                rel=str(attrs.get("rel", "")).split(),
                source_url=source_url,
                host=parsed.hostname.lower(),
                is_same_host=parsed.hostname.lower() == source_host,
            )
        )
    return links, total_count, total_count > len(links)


# ---------------------------------------------------------------------------
# Katana-inspired advanced discovery
# ---------------------------------------------------------------------------


_JS_ENDPOINT_RE = re.compile(
    r"""(?:fetch|axios\.\w+|jQuery\.ajax|\$\.ajax|\$\.get|\$\.post)\s*\(\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_XHR_OPEN_RE = re.compile(
    r"""(?:xhr|xmlhttprequest)\.open\s*\(\s*["'][A-Z]+["'],\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def extract_js_endpoints(html: str | None, source_url: str | None = None) -> list[dict[str, str]]:
    """Extract client-side API endpoints referenced from JavaScript.

    Katana-style discovery: find ``fetch(...)``, ``axios.*(...)``, jQuery
    ``$.get/$.post/$.ajax``, and ``XMLHttpRequest.open(...)`` calls so the
    service can surface dynamic backend endpoints for callers to queue.

    Each match is returned as ``{"url": <resolved absolute URL>}``. When
    ``source_url`` is supplied the endpoint is resolved against it; otherwise
    the raw endpoint string is returned unresolved. Results are de-duplicated
    by their resolved URL.
    """
    if not html:
        return []
    endpoints: list[dict[str, str]] = []
    seen: set[str] = set()
    for pattern in (_JS_ENDPOINT_RE, _XHR_OPEN_RE):
        for match in pattern.finditer(html):
            endpoint = match.group(1).strip()
            if not endpoint:
                continue
            resolved = urljoin(source_url, endpoint) if source_url else endpoint
            if resolved in seen:
                continue
            seen.add(resolved)
            endpoints.append({"url": resolved})
    return endpoints


_NEXT_REL_ANCHOR_RE = re.compile(r"<a\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_NEXT_REL_LINK_RE = re.compile(r"<link\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _detect_pagination(html: str | None, final_url: str | None) -> str | None:
    """Auto-detect the next-page URL via ``rel="next"`` hints.

    Checks both ``<a rel="next" href="...">`` and
    ``<link rel="next" href="...">`` and resolves the href against the
    final page URL. Returns ``None`` when no pagination hint is present.
    """
    if not html or not final_url:
        return None
    for container_re in (_NEXT_REL_ANCHOR_RE, _NEXT_REL_LINK_RE):
        for tag in container_re.finditer(html):
            rels = tag.group(1).lower().split()
            if "next" not in rels:
                continue
            href_match = _HREF_RE.search(tag.group(0))
            if href_match:
                return urljoin(final_url, href_match.group(1).strip())
    return None
