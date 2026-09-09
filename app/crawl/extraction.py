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
import multiprocessing
import os
import re
import sys
import threading
from html import unescape
from html.parser import HTMLParser
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

from app.crawl.firecrawl_format import _text_from_html
from app.models import DiscoveredLink
from app.validation import normalize_public_http_url

logger = logging.getLogger(__name__)

# Shared thread pool for extraction tasks to avoid thread leaks (Fix 4).
# Bounded to 4 workers to limit resource consumption.
_EXTRACTION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="extraction")

# Default 2 keeps memory bounded: each worker loads markitdown + parsers and the
# production container runs with a 3g mem limit (compose pins `mem_limit: 3g`).
# Raise via BROWSER_CRAWLER_EXTRACTION_DOC_WORKERS when more PDF/Office
# throughput is needed and the memory budget allows it.
_DEFAULT_DOC_EXTRACTION_WORKERS = 2


def _doc_extraction_worker_count() -> int:
    """Configured concurrency for markitdown extraction (process semaphore size)."""
    try:
        return max(1, int(os.environ.get("BROWSER_CRAWLER_EXTRACTION_DOC_WORKERS", str(_DEFAULT_DOC_EXTRACTION_WORKERS))))
    except ValueError:
        return _DEFAULT_DOC_EXTRACTION_WORKERS


# Document extraction (markitdown) runs in a SEPARATE PROCESS that is forcefully
# killed on timeout (adversarial perf #3.2). A persistent process pool cannot
# target a single timed-out future for termination, so each call spawns its own
# short-lived process; a semaphore bounds how many run at once so memory stays
# within the container ceiling. Defined after the worker-count helper above.
_DOC_SEM = threading.Semaphore(_doc_extraction_worker_count())


def _run_markitdown_in_process(body: bytes, conn) -> None:
    """Worker-process entry: run markitdown and ship the result over ``conn``."""
    # Support for test-induced child death (M8): exit without sending, as a
    # worker segfaulting on hostile input would. Inherited via env so it
    # works under both fork and spawn start methods.
    if os.environ.get("TEST_MARKITDOWN_DIE") == "1":
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110 - dying child, nothing to log to
            pass
        os._exit(1)
    try:
        res = _markitdown_extract(body)
        conn.send(("ok", res))
    except Exception:  # noqa: BLE001
        conn.send(("err", None))
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _terminate_doc_process(proc) -> None:
    """Force-kill a runaway document-extraction process and reap it."""
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.join(timeout=5)
    except Exception:  # noqa: BLE001
        pass


def _markitdown_extract(body: bytes) -> str | None:
    """Top-level picklable function for markitdown extraction in a worker process.

    Runs in a separate process so it can be forcefully killed on timeout.
    """
    # Support for test-induced hang simulation
    if os.environ.get("TEST_MARKITDOWN_HANG") == "1":
        import time
        time.sleep(10)  # Longer than any test timeout

    # Support for test output injection
    test_output = os.environ.get("TEST_MARKITDOWN_OUTPUT")
    if test_output is not None:
        return test_output

    try:
        from markitdown import MarkItDown
    except ImportError:
        return None
    res = MarkItDown().convert_stream(io.BytesIO(body))
    return res.text_content if res is not None else None


async def _shutdown_doc_extraction_executor() -> None:
    """No persistent document-extraction pool to shut down.

    Each extraction spawns its own short-lived process (reaped on completion or
    timeout), so nothing needs tearing down on service shutdown. Kept as a
    no-op so any (future) shutdown caller stays valid.
    """


# Wall-clock bound (seconds) for a single CSS/XPath selector evaluation.
# A pathological selector (or adversarial payload) could otherwise run
# unbounded and tie up a request worker. Override via env var.
_EXTRACTION_SELECTOR_TIMEOUT = float(
    os.environ.get("BROWSER_CRAWLER_EXTRACTION_SELECTOR_TIMEOUT", "2.0")
)

# Wall-clock bound (seconds) for document extraction (markitdown).
# Override via BROWSER_CRAWLER_EXTRACTION_DOC_TIMEOUT.
_EXTRACTION_DOC_TIMEOUT = float(
    os.environ.get("BROWSER_CRAWLER_EXTRACTION_DOC_TIMEOUT", "10.0")
)

# #mem-guard: a markitdown worker process shares the container cgroup; an
# unbounded document can balloon it past the memory ceiling. Cap the input
# size and allow deployments to disable subprocess extraction entirely.
_EXTRACTION_DOC_MAX_INPUT_BYTES = int(
    os.environ.get("BROWSER_CRAWLER_EXTRACTION_DOC_MAX_BYTES", str(20 * 1024 * 1024))
)
_EXTRACTION_DOC_ENABLED = os.environ.get(
    "BROWSER_CRAWLER_EXTRACTION_DOC_ENABLED", "true"
).lower() not in ("0", "false", "no")

# Maximum extracted markdown size (chars) to prevent unbounded output (F5).
_MAX_EXTRACTED_MARKDOWN = 5 * 1024 * 1024  # 5 MB


_EXTRACTION_SELECTOR_SENTINEL = object()


def _eval_selector(fn, timeout):
    """Run a selector evaluation in a worker thread so it cannot hang a request.

    Returns the callable's result, or ``_EXTRACTION_SELECTOR_SENTINEL`` if it
    exceeded ``timeout`` seconds.

    Uses a shared bounded thread pool to avoid thread leaks (Fix 4).
    """
    fut = _EXTRACTION_EXECUTOR.submit(fn)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return _EXTRACTION_SELECTOR_SENTINEL


_PDF_MAGIC = b"%PDF"
_DOC_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx",
    ".csv", ".json", ".xml", ".epub",
)


class ExtractedContent(NamedTuple):
    text: str | None
    markdown: str | None


def _decode(body: bytes | bytearray | str) -> str:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", "replace")
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
        body = bytes(content) if isinstance(content, (bytes, bytearray, memoryview)) else content.encode("utf-8", "replace")
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
    """Extract markdown from document bytes with a wall-clock timeout (F5).

    The markitdown conversion runs in a worker PROCESS with a hard timeout so
    a pathological document cannot block the worker indefinitely and the
    process can be forcefully killed. Output is also capped to prevent
    unbounded memory growth.
    """
    if not _EXTRACTION_DOC_ENABLED:
        return ExtractedContent(None, None)
    if len(body) > _EXTRACTION_DOC_MAX_INPUT_BYTES:
        logger.warning(
            "extraction_document_too_large",
            extra={"size_bytes": len(body), "max_bytes": _EXTRACTION_DOC_MAX_INPUT_BYTES},
        )
        return ExtractedContent(None, None)

    # Run markitdown in a worker process with a hard wall-clock timeout; on
    # timeout the runaway process is forcefully terminated so it cannot linger
    # as a zombie and starve the (bounded) extraction concurrency
    # (adversarial perf #3.2). A semaphore bounds concurrent processes so memory
    # stays within the container ceiling.
    _DOC_SEM.acquire()
    try:
        parent_conn, child_conn = multiprocessing.Pipe()
        proc = multiprocessing.Process(
            target=_run_markitdown_in_process, args=(body, child_conn)
        )
        proc.start()
        child_conn.close()  # parent keeps only its end of the pipe
        md = None
        try:
            if parent_conn.poll(_EXTRACTION_DOC_TIMEOUT):
                try:
                    _, md = parent_conn.recv()
                except (EOFError, OSError):
                    # The child died before sending (segfault on hostile
                    # input): poll() reports EOF as readable, then recv()
                    # raises. Graceful empty extraction, not an unhandled
                    # error -- and reap the child on every path (M8).
                    logger.warning("extraction_document_child_died")
                    _terminate_doc_process(proc)
                    return ExtractedContent(None, None)
            else:
                logger.warning("extraction_document_timeout")
                _terminate_doc_process(proc)
                return ExtractedContent(None, None)
        finally:
            if proc.is_alive():
                proc.join(timeout=2)
            parent_conn.close()
    finally:
        _DOC_SEM.release()

    if not md or not md.strip():
        return ExtractedContent(None, None)

    # Cap output size (F5).
    if len(md) > _MAX_EXTRACTED_MARKDOWN:
        md = md[:_MAX_EXTRACTED_MARKDOWN]
        logger.warning(
            "extraction_document_truncated", extra={"max_chars": _MAX_EXTRACTED_MARKDOWN}
        )
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

    Kept because the test-suite builds it directly via
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


def _extract_frames(
    html: str | None, source_url: str | None, *, tree: Any | None = None
) -> list[dict[str, Any]]:
    if not html or not source_url:
        return []
    source_host = (urlparse(source_url).hostname or "").lower()
    if tree is None:
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


def _extract_forms(
    html: str | None, source_url: str | None, *, tree: Any | None = None
) -> list[dict[str, Any]]:
    if not html or not source_url:
        return []
    if tree is None:
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


def _html_diagnostics(
    html: str | None, source_url: str | None, *, tree: Any | None = None
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    # #11: parse the HTML once and share the single lxml tree across the
    # frames/forms passes instead of reparsing per pass.
    if tree is None:
        tree = _parse_html_tree(html)
    frames = _extract_frames(html, source_url, tree=tree)
    forms = _extract_forms(html, source_url, tree=tree)
    if frames:
        diagnostics["frames"] = frames
    if forms:
        diagnostics["forms"] = forms
    return diagnostics




def _extract_discovered_links(
    html: str | None, source_url: str | None, *, max_links: int = 250, tree: Any | None = None
) -> tuple[list[Any], int, bool]:
    if not html or not source_url:
        return [], 0, False
    try:
        norm_source = normalize_public_http_url(source_url)
    except ValueError:
        norm_source = source_url
    source_host = (urlparse(norm_source).hostname or "").lower()
    if tree is None:
        tree = _parse_html_tree(html)
    if tree is None:
        return [], 0, False
    # Single pass: normalize/dedupe inline instead of materializing an
    # intermediate per-anchor dict list (memory on link-heavy pages).
    links: list[Any] = []
    seen: set[str] = set()
    total_count = 0
    for el in tree.iter("a"):
        # Skip anchors nested inside another anchor to avoid double counting.
        parent = el.getparent()
        if parent is not None and str(parent.tag) == "a":
            continue
        attrs = {key.lower(): value for key, value in el.attrib.items()}
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
        text = re.sub(r"\s+", " ", unescape("".join(el.itertext()))).strip()
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
