"""Document and selector extraction over pooled workers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, TypeVar

from lxml import etree
from markdownify import markdownify as html_to_md
from trafilatura import extract as _trafilatura_extract

from app.config.env import get_env_int, parse_bool_env
from app.config.settings import CrawlerSettings
from app.extraction.doc_pool import DocExtractionPool, doc_task_hooks
from app.extraction.llm_format import text_from_html
from app.extraction.trees import parse_html_tree
from app.models.normalized_url import NormalizedUrl
from app.runtime.resource_pressure import doc_worker_count

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Selectors:
    """A narrowing request: the part of a page the caller wants extracted, as one
    value (CSS and XPath are two spellings of one intent, only ever sent together,
    so half a narrowing request is inexpressible). Frozen and slots: settled intent.
    """

    css: str | None = None
    xpath: str | None = None

    def __bool__(self) -> bool:
        """True when the request actually narrows. ``Selectors()`` is no request."""
        return self.css is not None or self.xpath is not None


# Shared selector-extraction pool; built on demand and torn
# down to ``None`` by shutdown_doc_extraction_executor.
_EXTRACTION_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


# Dedicated pool for the DOCUMENT branch: a blocked ``DOC_POOL.acquire`` wait lives
# here, never on the shared default executor the store control plane needs.
_DOC_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _doc_executor() -> concurrent.futures.ThreadPoolExecutor:
    """The document branch's own pool, created on first use, sized to the worker
    count and torn down with extraction on shutdown."""
    global _DOC_EXECUTOR
    if _DOC_EXECUTOR is None:
        _DOC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=doc_worker_count(), thread_name_prefix="doc-extract"
        )
    return _DOC_EXECUTOR


def extraction_thread_count() -> int:
    """Pool size for selector extraction: the operator's knob, else the CPU."""
    try:
        return get_env_int("CRAWLUTRON_EXTRACTION_THREADS", max(4, os.cpu_count() or 4), lo=1)
    except ValueError:
        return max(4, os.cpu_count() or 4)


def _extraction_executor() -> concurrent.futures.ThreadPoolExecutor:
    """The shared pool, created on demand; recreate-on-demand is the documented
    lifespan-restart contract, so shutdown ends threads, not extraction.
    """
    global _EXTRACTION_EXECUTOR
    if _EXTRACTION_EXECUTOR is None:
        _EXTRACTION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=extraction_thread_count(), thread_name_prefix="extraction"
        )
    return _EXTRACTION_EXECUTOR


def _document_extraction_enabled() -> bool:
    return parse_bool_env("CRAWLUTRON_EXTRACTION_DOC_ENABLED", default=True)


# Real workers, configured count, environment opt-out -- every knob is a
# constructor argument, so no caller reaches into another module's state.
def build_doc_pool() -> DocExtractionPool:
    """A document pool configured the way this module configures its own.

    The module's pool is one of these. A caller that needs a pool of its own
    builds one the same way rather than reaching for the module's.
    """
    return DocExtractionPool(
        worker_count=doc_worker_count,
        enabled=_document_extraction_enabled,
    )


DOC_POOL = build_doc_pool()


async def shutdown_doc_extraction_executor() -> None:
    """Terminate every persistent extraction worker, process and thread alike.
    Both pools shut down here, once: the caller used to check them separately
    and had to know there were two, so a new pool could ship never closed.
    """
    DOC_POOL.teardown()
    global _EXTRACTION_EXECUTOR, _DOC_EXECUTOR
    executor, _EXTRACTION_EXECUTOR = _EXTRACTION_EXECUTOR, None
    if executor is not None:
        # Refuse queued-but-unstarted work, then await every running future:
        # no extraction thread outlives the shutdown.
        executor.shutdown(wait=True, cancel_futures=True)
    doc_executor, _DOC_EXECUTOR = _DOC_EXECUTOR, None
    if doc_executor is not None:
        doc_executor.shutdown(wait=True, cancel_futures=True)


# Extraction deadlines live in CrawlerSettings (env-mapped, operator-tunable).
# #mem-guard: workers share the cgroup -- cap input size, allow disabling extraction.
EXTRACTION_DOC_MAX_INPUT_BYTES = get_env_int("CRAWLUTRON_EXTRACTION_DOC_MAX_BYTES", 20 * 1024 * 1024, lo=1)


class ExtractedContent(NamedTuple):
    text: str | None
    markdown: str | None


def html_to_markdown(html_fragment: str) -> tuple[str | None, str | None]:
    """Convert a raw HTML fragment to ``(text, markdown)`` for DOM targeting."""
    try:
        md = html_to_md(html_fragment) or None
    except Exception:
        md = None
    text = text_from_html(html_fragment) or None
    return text, md


class _SelectorTimedOut:
    """Type of the timeout sentinel, so a timeout does not erase the result type.
    A bare object() would collapse the evaluator's ``T | object`` to object, so the
    caller's isinstance check narrows to the callable's real return (``is`` alone cannot narrow).
    """

    __slots__ = ()


_EXTRACTION_SELECTOR_SENTINEL = _SelectorTimedOut()


_SelectorResult = TypeVar("_SelectorResult")


def _eval_selector[SelectorResult](
    fn: Callable[[], SelectorResult], timeout: float
) -> SelectorResult | _SelectorTimedOut:
    """Run a selector evaluation in a worker thread so it cannot hang a request.
    Returns the callable's result, or ``_EXTRACTION_SELECTOR_SENTINEL`` when it
    exceeds ``timeout``, using the shared bounded pool to avoid thread leaks.
    """
    fut = _extraction_executor().submit(fn)
    try:
        return fut.result(timeout=timeout)
    except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
        # Cancel once the runaway finishes; without this four pathological
        # selectors pin the pool. CancelledError: shutdown refused the work.
        fut.cancel()
        concurrent.futures.wait([fut], timeout=timeout)
        return _EXTRACTION_SELECTOR_SENTINEL


def extract_selected(
    html: str,
    source_url: NormalizedUrl | None,
    *,
    selectors: Selectors | None = None,
    settings: CrawlerSettings | None = None,
    tree: Any | None = None,
) -> ExtractedContent:
    """Mechanical DOM targeting: extract only matched nodes as Markdown, via a
    Selectors value (never loose css/xpath params), CSS by BeautifulSoup and
    XPath by lxml; ``tree`` accepts an already-parsed lxml tree so a holder pays one parse.
    """
    if not html or not selectors:
        return ExtractedContent(None, None)
    # One evaluation deadline for every selector form. A pathological selector or
    # a hostile payload could otherwise run unbounded and tie up a request worker.
    selector_timeout = (settings or CrawlerSettings.from_mapping()).extraction_selector_timeout
    if selectors.css:
        fragment = _css_selected_fragment(html, selectors, selector_timeout)
    elif selectors.xpath:
        fragment = _xpath_selected_fragment(html, selectors, selector_timeout, tree=tree)
    else:
        fragment = None
    if not fragment:
        return ExtractedContent(None, None)
    text, md = html_to_markdown(fragment)
    return ExtractedContent(md or text, md or text)


def _css_selected_fragment(html: str, selectors: Selectors, selector_timeout: float) -> str | None:
    """The CSS form: BeautifulSoup select under the shared deadline."""
    css_selector = selectors.css or ""
    if not css_selector:
        return None
    if len(html) > EXTRACTION_DOC_MAX_INPUT_BYTES:
        # The parse itself is untimed on the caller thread: refuse a hostile-size
        # payload instead of parsing it.
        logger.warning(
            "extraction_css_payload_too_large",
            extra={"size_bytes": len(html), "max_bytes": EXTRACTION_DOC_MAX_INPUT_BYTES},
        )
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    soup = BeautifulSoup(html, "html.parser")
    selected = _eval_selector(lambda: soup.select(css_selector), selector_timeout)
    if isinstance(selected, _SelectorTimedOut):
        logger.warning(
            "extraction_selector_timeout",
            extra={"selector_type": "css", "selector": css_selector},
        )
        return None
    if not selected:
        return None
    return "\n".join(str(tag) for tag in selected)


def _xpath_selected_fragment(
    html: str, selectors: Selectors, selector_timeout: float, *, tree: Any | None
) -> str | None:
    """The XPath form: lxml under the shared deadline; invalid XPath is caller error."""
    xpath = selectors.xpath or ""
    if not xpath:
        return None
    tree = tree if tree is not None else parse_html_tree(html)
    if tree is None:
        return None
    try:
        nodes = _eval_selector(lambda: tree.xpath(xpath), selector_timeout)
    except Exception:  # invalid XPath is caller error, not extraction
        return None
    if isinstance(nodes, _SelectorTimedOut):
        logger.warning(
            "extraction_selector_timeout",
            extra={"selector_type": "xpath", "selector": xpath},
        )
        return None
    if not nodes:
        return None
    parts: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            parts.append(node)
        else:
            parts.append(etree.tostring(node, encoding="unicode"))
    return "\n".join(parts)


PDF_MAGIC = b"%PDF"


DOC_EXTENSIONS = (
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".csv",
    ".json",
    ".xml",
    ".epub",
)


class _LogRestorer:
    """Detach the filters a :func:`suppress_trafilatura_chatter` call installed.
    Module-level so the suppressor can declare it as its return type -- an inner
    class would leave the ``with`` block typing against a bare ``object``.
    """

    def __init__(self, filters: Sequence[tuple[logging.Logger, logging.Filter]]) -> None:
        self._filters = filters

    def __enter__(self) -> _LogRestorer:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        for log, log_filter in self._filters:
            log.removeFilter(log_filter)
        return False


class _DropBelowCritical(logging.Filter):
    """Drop every record while installed: suppression by filter, not by level,
    so concurrent suppressions cannot snapshot and pin each other's levels.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return False


def suppress_trafilatura_chatter() -> _LogRestorer:
    """Silence trafilatura's ERROR chatter for benign inputs (empty/fragment/no-main
    HTML), which carries no diagnostic signal and otherwise drowns real failures
    at scale; a context manager so suppression is scoped to the extraction call.
    """
    loggers = [logging.getLogger(n) for n in ("trafilatura.core", "trafilatura.utils", "trafilatura.xml")]
    dropper = _DropBelowCritical()
    installed = [(log, dropper) for log in loggers]
    for log, log_filter in installed:
        log.addFilter(log_filter)
    return _LogRestorer(installed)


def extract_html(
    html: str,
    url: NormalizedUrl | None,
    selectors: Selectors | None = None,
) -> ExtractedContent:
    """The single owner of the HTML selector dispatch, for both callers: the unified
    extract_content entry and the trafilatura-only path both route HTML here, so
    one place decides selector-targeted vs full-page and they cannot disagree.
    """
    if selectors:
        return extract_selected(html, url, selectors=selectors)
    try:
        with suppress_trafilatura_chatter():
            md = _trafilatura_extract(
                html,
                url=url.raw if url is not None else None,
                output_format="markdown",
                favor_recall=True,
                include_comments=False,
                include_tables=True,
                include_formatting=True,
                include_links=True,
                deduplicate=True,
            )
    except Exception:
        md = None
    if not md or not md.strip():
        # Fallback to markdownify before the plain-text extractor so that
        # pages Trafilatura refuses still become usable Markdown.
        try:
            md = html_to_md(html) or None
        except Exception:
            md = None
    if not md or not md.strip():
        md = text_from_html(html) or None
    return ExtractedContent(md, md)


def extract_document(
    body: bytes, url: NormalizedUrl | None, settings: CrawlerSettings | None = None
) -> ExtractedContent:
    """Extract markdown from document bytes with a wall-clock timeout.
    The conversion runs on a persistent pool worker with a hard per-task timeout,
    so a pathological document cannot block the pool; the worker is killed, its slot respawned, output capped.
    """
    active = settings or CrawlerSettings.from_mapping()
    if not _document_extraction_enabled():
        return ExtractedContent(None, None)
    if len(body) > EXTRACTION_DOC_MAX_INPUT_BYTES:
        logger.warning(
            "extraction_document_too_large",
            extra={"size_bytes": len(body), "max_bytes": EXTRACTION_DOC_MAX_INPUT_BYTES},
        )
        return ExtractedContent(None, None)

    # Pool task with a hard wall-clock timeout; on timeout or worker death the
    # runaway is terminated and its slot respawned, bounding memory to the ceiling.
    worker = DOC_POOL.acquire(active.extraction_doc_acquire_timeout)
    if worker is None:
        # No slot within the acquire budget: degrade this document like every
        # other document failure instead of wedging the request.
        return ExtractedContent(None, None)
    try:
        md = _document_pool_exchange(
            worker, body, active.extraction_doc_timeout, cap=active.extraction_max_markdown
        )
    finally:
        DOC_POOL.release(worker)

    if not md or not md.strip():
        return ExtractedContent(None, None)

    # Defense in depth only: the worker already capped before send, so a reply
    # over cap here means an old worker or a foreign pipe -- name it, cut it.
    cap = active.extraction_max_markdown
    if len(md) > cap:
        md = md[:cap]
        logger.warning("extraction_document_truncated", extra={"max_chars": cap})
    return ExtractedContent(md, md)


def _document_pool_exchange(worker, body: bytes, timeout_seconds: float, cap: int) -> str | None:
    """Send, wait, receive -- retiring the slot on every death or timeout path.

    The cap rides the task so the worker truncates before send. Death
    and timeout paths each retire the slot and report empty.
    """
    try:
        worker.conn.send((body, doc_task_hooks(), cap))
    except (EOFError, OSError):
        # Send raced a worker death: reap the slot and report empty.
        logger.warning("extraction_document_child_died")
        DOC_POOL.retire(worker)
        return None
    try:
        ready = worker.conn.poll(timeout_seconds)
    except (EOFError, OSError):
        logger.warning("extraction_document_child_died")
        DOC_POOL.retire(worker)
        return None
    if not ready:
        logger.warning("extraction_document_timeout")
        DOC_POOL.retire(worker)
        return None
    try:
        _, md = worker.conn.recv()
        return md
    except (EOFError, OSError):
        # Worker died pre-send (segfault on hostile input): EOF reads as
        # readable, then recv() raises. Empty extraction; retire the slot.
        logger.warning("extraction_document_child_died")
        DOC_POOL.retire(worker)
        return None


async def adocument_extract(
    body: bytes, url: NormalizedUrl | None, settings: CrawlerSettings | None = None
) -> ExtractedContent:
    """Run one document extraction on the dedicated doc pool, off the default
    executor: a blocked ``DOC_POOL.acquire`` wait lives here and here only, so the
    store control plane's ``asyncio.to_thread`` calls are never starved by a crawl.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _doc_executor(), functools.partial(extract_document, body, url, settings)
    )
