"""Document and selector extraction over pooled workers."""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, NamedTuple, TypeVar

from lxml import etree
from markdownify import markdownify as html_to_md
from trafilatura import extract as _trafilatura_extract

from app.config.env import get_env_int, parse_bool_env
from app.config.settings import CrawlerSettings
from app.extraction.llm_format import text_from_html
from app.extraction.trees import parse_html_tree
from app.models.normalized_url import NormalizedUrl
from app.orchestration.doc_pool import DocExtractionPool, doc_task_hooks
from app.runtime.resource_pressure import doc_worker_count

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Selectors:
    """A narrowing request: the part of a page the caller wants extracted.

    CSS and XPath are two spellings of one intent, and they are only ever sent
    together -- a request carrying a CSS selector and no XPath is not a narrower
    request, it is half of one, and half a narrowing request is what a function
    that takes the two names separately is able to express. As two optional
    parameters on each of three extractors, that mistake had a spelling
    everywhere and a rule somewhere else forbidding it; as one value it has no
    spelling at all, so the rule stopped being necessary and became structural.

    Frozen and slots because it is a settled statement of intent, built once by
    the caller from policy and read by everything downstream: nothing here edits
    a narrowing request, and a mutable one would let one extractor narrow and
    another widen from the same object.
    """

    css: str | None = None
    xpath: str | None = None

    def __bool__(self) -> bool:
        """True when the request actually narrows. ``Selectors()`` is no request."""
        return self.css is not None or self.xpath is not None


# Shared extraction pool, bounded to 4 workers; built on demand and torn down
# to ``None`` by shutdown_doc_extraction_executor -- "not right now", not "finished".
_EXTRACTION_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None


def _extraction_executor() -> concurrent.futures.ThreadPoolExecutor:
    """The shared pool, created on first use.

    Every worker is idle before this returns: a pool handed out while a
    shutdown is in flight is a pool whose ``submit`` may land after it was
    closed, which is the same failure one layer down.
    """
    global _EXTRACTION_EXECUTOR
    if _EXTRACTION_EXECUTOR is None:
        _EXTRACTION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="extraction"
        )
    return _EXTRACTION_EXECUTOR


def _document_extraction_enabled() -> bool:
    return parse_bool_env("CRAWLUTRON_EXTRACTION_DOC_ENABLED", default=True)


# Real workers, configured count, environment opt-out -- every knob is a
# constructor argument, so no caller reaches into another module's state.
DOC_POOL = DocExtractionPool(
    worker_count=doc_worker_count,
    enabled=_document_extraction_enabled,
)


async def shutdown_doc_extraction_executor() -> None:
    """Terminate every persistent extraction worker, process and thread alike.

    Both pools were checked separately by the caller, which meant the caller had
    to know there were two. The thread pool has no shutdown of its own here, so
    asking whether it is still alive was the only way it ever got closed; a
    second pool added later would have shipped unclosed by exactly this route.
    """
    DOC_POOL.teardown()
    global _EXTRACTION_EXECUTOR
    executor, _EXTRACTION_EXECUTOR = _EXTRACTION_EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=False)


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

    The sentinel used to be a bare ``object()``, which forced the evaluator's
    return type to ``T | object`` -- and a union with ``object`` collapses to
    ``object`` at every call site. That erasure is why ``for tag in selected``
    type-checked as iterating an ``object``: the selector's real result type was
    thrown away before the loop could see it. A sentinel with its own class
    keeps the union narrow, so the caller's ``isinstance(..., _SelectorTimedOut)``
    check narrows the rest to the callable's actual return type. Identity is not
    enough here: pyright does not narrow a union on ``is`` against a module-level
    instance, so the timeout path stayed in the type and the selector loop below
    still type-checked as iterating a sentinel.
    """

    __slots__ = ()


_EXTRACTION_SELECTOR_SENTINEL = _SelectorTimedOut()


_SelectorResult = TypeVar("_SelectorResult")


def _eval_selector[SelectorResult](
    fn: Callable[[], SelectorResult], timeout: float
) -> SelectorResult | _SelectorTimedOut:
    """Run a selector evaluation in a worker thread so it cannot hang a request.

    Returns the callable's result, or ``_EXTRACTION_SELECTOR_SENTINEL`` if it
    exceeded ``timeout`` seconds.

    Uses a shared bounded thread pool to avoid thread leaks.
    """
    fut = _extraction_executor().submit(fn)
    try:
        return fut.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return _EXTRACTION_SELECTOR_SENTINEL


def extract_selected(
    html: str,
    source_url: NormalizedUrl | None,
    *,
    selectors: Selectors | None = None,
    settings: CrawlerSettings | None = None,
    tree: Any | None = None,
) -> ExtractedContent:
    """Mechanical DOM targeting: extract only the matched nodes as Markdown.

    One selector contract shared with ``extract_content`` and ``extract_html``:
    a :class:`Selectors` value, never loose ``css_selector``/``xpath`` parameters,
    so half a narrowing request is not expressible. CSS selectors are resolved
    with BeautifulSoup; XPath expressions with lxml.
    The matched subtree(s) are serialized and converted to Markdown, so callers
    can target a specific section (e.g. an agenda table) without Trafilatura's
    full-page boilerplate removal.

    ``tree`` accepts an already-parsed lxml tree for the XPath form, the same
    seam ``html_diagnostics`` offers: a caller that already holds the tree pays
    for one parse, and a test can hand in the tree whose evaluation it wants
    bounded instead of replacing the parser.
    """
    if not html or not selectors:
        return ExtractedContent(None, None)
    # One evaluation deadline for every selector form. A pathological selector or
    # a hostile payload could otherwise run unbounded and tie up a request worker.
    selector_timeout = (settings or CrawlerSettings.from_mapping()).extraction_selector_timeout
    fragment: str | None = None
    css_selector = selectors.css
    if css_selector:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return ExtractedContent(None, None)
        soup = BeautifulSoup(html, "html.parser")
        selected = _eval_selector(lambda: soup.select(css_selector), selector_timeout)
        if isinstance(selected, _SelectorTimedOut):
            logger.warning(
                "extraction_selector_timeout",
                extra={"selector_type": "css", "selector": css_selector},
            )
            return ExtractedContent(None, None)
        if not selected:
            return ExtractedContent(None, None)
        fragment = "\n".join(str(tag) for tag in selected)
    elif selectors.xpath:
        xpath = selectors.xpath
        tree = tree if tree is not None else parse_html_tree(html)
        if tree is None:
            return ExtractedContent(None, None)
        try:
            nodes = _eval_selector(lambda: tree.xpath(xpath), selector_timeout)
        except Exception:  # invalid XPath is caller error, not extraction
            return ExtractedContent(None, None)
        if isinstance(nodes, _SelectorTimedOut):
            logger.warning(
                "extraction_selector_timeout",
                extra={"selector_type": "xpath", "selector": xpath},
            )
            return ExtractedContent(None, None)
        if not nodes:
            return ExtractedContent(None, None)
        parts: list[str] = []
        for node in nodes:
            if isinstance(node, str):
                parts.append(node)
            else:
                parts.append(etree.tostring(node, encoding="unicode"))
        fragment = "\n".join(parts)
    if not fragment:
        return ExtractedContent(None, None)
    text, md = html_to_markdown(fragment)
    return ExtractedContent(md or text, md or text)


_PDF_MAGIC = b"%PDF"


_DOC_EXTENSIONS = (
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
    """Restore logger levels a:func:`_suppress_trafilatura_chatter` call muted.

    A module-level class (rather than one defined inside the function) so the
    suppressor can declare it as its return type: an inner class would leave
    the caller with ``object``, which has no ``__enter__``/``__exit__`` to type
    the ``with`` block against.
    """

    def __init__(self, prior: dict[logging.Logger, int]) -> None:
        self._prior = prior

    def __enter__(self) -> _LogRestorer:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        for logger, level in self._prior.items():
            logger.setLevel(level)
        return False


def _suppress_trafilatura_chatter() -> _LogRestorer:
    """Silence trafilatura's ERROR-level chatter for benign inputs.

    trafilatura logs ERROR from ``trafilatura.core`` and ``trafilatura.utils``
    when the HTML is empty, a fragment, or has no main content. All three are
    normal for a crawler (a robots page, a redirect stub, an empty 200), and
    ``extract_html`` already falls through to markdownify and then plain
    text, so the record carries no diagnostic signal. At ERROR level across a
    ~2000-page run it produced 2239 records that crowded out real failures.

    Used as a context manager so the suppression is scoped to the extraction
    call: a parse failure we did not route here stays loud.
    """
    loggers = [logging.getLogger(n) for n in ("trafilatura.core", "trafilatura.utils", "trafilatura.xml")]
    prior = {logger: logger.level for logger in loggers}
    for logger in loggers:
        logger.setLevel(logging.CRITICAL)
    return _LogRestorer(prior)


def extract_html(
    html: str,
    url: NormalizedUrl | None,
    selectors: Selectors | None = None,
) -> ExtractedContent:
    """The single owner of the HTML selector dispatch, for both callers.

    The unified :func:`extract_content` entry and the trafilatura-only strategy
    path both route HTML here, which is why the pair is a parameter on this
    function at all: one place decides whether a page is selector-targeted or
    full-page, so the two callers cannot disagree about the same page.
    """
    if selectors:
        return extract_selected(html, url, selectors=selectors)
    try:
        with _suppress_trafilatura_chatter():
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

    The markitdown conversion runs on a persistent pool worker with a hard
    per-task timeout so a pathological document cannot block the pool
    indefinitely; the worker is forcefully killed and its slot respawned.
    Output is also capped to prevent unbounded memory growth.
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
        try:
            worker.conn.send((body, doc_task_hooks()))
        except (EOFError, OSError):
            # Send raced a worker death: reap the slot and report empty.
            logger.warning("extraction_document_child_died")
            DOC_POOL.retire(worker)
            return ExtractedContent(None, None)
        try:
            ready = worker.conn.poll(active.extraction_doc_timeout)
        except (EOFError, OSError):
            logger.warning("extraction_document_child_died")
            DOC_POOL.retire(worker)
            return ExtractedContent(None, None)
        md = None
        if ready:
            try:
                _, md = worker.conn.recv()
            except (EOFError, OSError):
                # Worker died pre-send (segfault on hostile input): EOF reads as
                # readable, then recv() raises. Empty extraction; retire the slot.
                logger.warning("extraction_document_child_died")
                DOC_POOL.retire(worker)
                return ExtractedContent(None, None)
        else:
            logger.warning("extraction_document_timeout")
            DOC_POOL.retire(worker)
            return ExtractedContent(None, None)
    finally:
        DOC_POOL.release(worker)

    if not md or not md.strip():
        return ExtractedContent(None, None)

    # Cap output size. Settings carry the cap so a caller bounding one document
    # states the bound instead of rewriting this module's constant.
    cap = active.extraction_max_markdown
    if len(md) > cap:
        md = md[:cap]
        logger.warning("extraction_document_truncated", extra={"max_chars": cap})
    return ExtractedContent(md, md)
