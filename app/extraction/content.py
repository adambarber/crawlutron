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
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from html import unescape
from typing import Any, Literal, NamedTuple, TypeVar

from lxml import etree
from lxml.etree import ParserError
from markdownify import markdownify as html_to_md
from trafilatura import extract as _trafilatura_extract

# Lxml does not mark re-exports, so importing a name that IS present at runtime is
# reported as a private import; `reportPrivateImportUsage` is off in
# pyrightconfig.json for that reason. These carried
# `reportAttributeAccessIssue` suppressions on the theory that lxml ships partial
# stubs. They suppressed nothing, and `reportUnnecessaryTypeIgnoreComment` (now
# on) reports them as unnecessary. Imported at the top because lxml is a declared
# dependency: the two function-local imports these replace each carried a
# `try/except ImportError` that returned "no content" for a package that cannot
# be absent.
from app.crawl.doc_pool import DocExtractionPool, doc_task_hooks
from app.env import get_env_int, parse_bool_env
from app.extraction.llm_format import (
    PageMetadata as ExtractedPageMetadata,
)
from app.extraction.llm_format import _llm_metadata_from_tree, text_from_html
from app.models.discovered_link import DiscoveredLink
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.resource_pressure import doc_worker_count
from app.settings import CrawlerSettings
from app.validation import filter_js_endpoints

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


# Shared thread pool for extraction tasks to avoid thread leaks.
# Bounded to 4 workers to limit resource consumption.
#
# Built on demand and torn down to ``None`` by
# ``shutdown_doc_extraction_executor``, exactly as the process pool beside it
# behaves: a shutdown means "not right now", not "this module is finished". An
# executor built once at import and shut down in place is a tombstone -- every
# later selector evaluation raises ``cannot schedule new futures after
# shutdown`` -- and because the shutdown is wired into the app lifespan, any
# caller that starts and stops the app once kills extraction for the rest of the
# process.
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


# Document extraction (markitdown) runs in a PERSISTENT pool of worker
# processes; the pool and its machinery live in app.crawl.doc_pool. This module
# owns the extraction pipeline, and holds one process-lifetime pool built from
# the environment. Sized by doc_worker_count() (app.resource_pressure -- the
# single home for the count) so memory stays within the container ceiling.


def _document_extraction_enabled() -> bool:
    return parse_bool_env("CRAWLUTRON_EXTRACTION_DOC_ENABLED", default=True)


# The production pool: real workers, the configured count, the environment's
# opt-out. Every knob the pool has is a constructor argument rather than a
# module name, so a caller that needs a different arrangement builds its own
# pool instead of reaching into another module's state.
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


# The three extraction deadlines (one selector evaluation, one document task in
# its worker, and the wait for a free worker slot) live in CrawlerSettings, which
# is where they get their env mapping and their operators. They used to sit here
# as module constants read once at import, which made a deadline no caller could
# construct and put fourteen private patch sites in the suite for three numbers.

# #mem-guard: a markitdown worker process shares the container cgroup; an
# unbounded document can balloon it past the memory ceiling. Cap the input
# size and allow deployments to disable subprocess extraction entirely.
EXTRACTION_DOC_MAX_INPUT_BYTES = get_env_int("CRAWLUTRON_EXTRACTION_DOC_MAX_BYTES", 20 * 1024 * 1024, lo=1)

# Maximum extracted markdown size (chars) to prevent unbounded output.


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


class ExtractedContent(NamedTuple):
    text: str | None
    markdown: str | None


def _decode(body: bytes | bytearray | str) -> str:
    if isinstance(body, (bytes, bytearray)):
        return bytes(body).decode("utf-8", "replace")
    return body


def is_html(content_type: str | None, body: bytes | str) -> bool:
    ct = (content_type or "").lower()
    if "html" in ct:
        return True
    text = _decode(body).lstrip().lower()
    return text.startswith("<html") or text.startswith("<!doctype html>")


def looks_like_document(content_type: str | None, body: bytes | str, url: NormalizedUrl | None) -> bool:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return True
    if "html" in ct:
        return False
    lower_url = (url.raw if url is not None else "").split("?")[0].lower()
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
    url: NormalizedUrl | None,
    selectors: Selectors | None = None,
) -> ExtractedContent:
    """Extract ``(text, markdown)`` from raw content using the unified pipeline.

    The HTML branch hands the selector pair down rather than dispatching on it
    here: ``extract_html`` already routes a selector to :func:`extract_selected`
    and a bare page to trafilatura, so deciding it in both places was two copies
    of one rule that could disagree about the same page.

    A selector only means something to a DOM. Applied to a document or a plain
    text body there is no DOM to target, so the pair cannot be honoured and the
    full content is extracted instead. That is logged rather than dropped in
    silence: the caller asked to narrow and is being handed the whole thing, and
    the only other signal it gets is a result that looks like a successful
    narrowing. The same request against an HTML page does narrow, so the
    difference between the two is invisible on the wire otherwise.
    """
    # ONE decision, read twice -- by the drop report below and by the dispatch
    # after it. The predicate is not free: it decodes the whole body, strips and
    # lowercases it, and on a binary/document response the two halves below
    # disagree about nothing, so evaluating it twice was a second full decode per
    # non-HTML request (a capped multi-megabyte PDF pays it twice) to learn the
    # same thing. It was also unreadable as written: `elif not is_html(...)` and
    # `if is_html(...)` below are the same question asked in opposite polarity,
    # and nothing in the text said they had to agree. Now there is one answer and
    # two consumers, so a report and a dispatch cannot describe different bodies.
    html_body = is_html(content_type, content)
    if selectors and not html_body:
        logger.warning(
            "extraction_selector_ignored_non_html",
            extra={
                "css_selector": selectors.css,
                "xpath": selectors.xpath,
                "content_type": content_type,
                "url": url.raw if url is not None else None,
            },
        )
    if html_body:
        return extract_html(_decode(content), url, selectors)
    if looks_like_document(content_type, content, url):
        body = (
            bytes(content)
            if isinstance(content, (bytes, bytearray, memoryview))
            else content.encode("utf-8", "replace")
        )
        return extract_document(body, url)
    text = _decode(content).strip()
    return ExtractedContent(text or None, text or None)


def html_to_markdown(html_fragment: str) -> tuple[str | None, str | None]:
    """Convert a raw HTML fragment to ``(text, markdown)`` for DOM targeting."""
    try:
        md = html_to_md(html_fragment) or None
    except Exception:
        md = None
    text = text_from_html(html_fragment) or None
    return text, md


# The selector pair is ONE contract with three entry points: ``extract_content``
# (every strategy's funnel), ``extract_html`` (the trafilatura-only path) and
# ``extract_selected`` (the DOM-targeting engine). It used to be four spellings:
# those three signatures, plus the ``{"css_selector": ..., "xpath": ...}`` mapping
# ``content_pipeline`` built and spread into two of them. Four copies of one fact
# with a rule elsewhere forbidding disagreement between them.
#
# :class:`Selectors` is the collapse. ``content_pipeline`` builds one from policy
# and hands it whole, so the pair is now stated once, in one type, and neither the
# signatures nor the mapping that feeds them spell it. The win is not the removed
# clone -- it is that "half a narrowing request" stopped being expressible. With
# two optional names per function, ``css_selector=...`` alone was a valid call at
# three signatures, accepted and then silently not honoured in full; there was no
# spelling of the mistake to forbid, only a detector. As one value there is no
# second parameter to forget.
#
# ``test_the_selector_pair_is_one_contract`` now proves the carrier rather than
# the count: an extractor must take a ``selectors`` parameter and must not declare
# either selector name itself, so a future fourth extractor cannot reintroduce the
# pair as parameters and quietly pass a guard that only knew the old rule.


def extract_selected(
    html: str,
    source_url: NormalizedUrl | None,
    *,
    selectors: Selectors | None = None,
    settings: CrawlerSettings | None = None,
    tree: Any | None = None,
) -> ExtractedContent:
    """Mechanical DOM targeting: extract only the matched nodes as Markdown.

    CSS selectors are resolved with BeautifulSoup; XPath expressions with lxml.
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

    # Serve the document from the persistent pool with a hard wall-clock
    # timeout per task; on timeout -- or if the worker died mid-task -- the
    # runaway worker is forcefully terminated and reaped so it cannot linger
    # as a zombie, and its slot is respawned on the
    # next acquisition. Pool size bounds concurrent processes so memory
    # stays within the container ceiling.
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
                # The worker died before sending (segfault on hostile
                # input): poll() reports EOF as readable, then recv()
                # raises. Graceful empty extraction, not an unhandled
                # error -- and retire the slot on every death path.
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


# ---------------------------------------------------------------------------
# Structured discovery diagnostics: links, frames, forms
# ---------------------------------------------------------------------------


# lxml parsing is invisible: the per-page parse budget is a real performance
# property, and nothing but a count can show a parse that stopped happening or
# started happening twice. The alternative -- replacing the parser and counting
# calls -- would measure a patched function rather than the real one, and would
# miss every parse the patch did not sit in front of. A lock because the
# pipeline parses from worker threads and a racy tally would make that budget
# flaky rather than merely imprecise.
_parse_tally_lock = threading.Lock()
_parse_tally = 0


def parse_count() -> int:
    """How many lxml trees this process has parsed.

    Cumulative and monotonic: a caller measuring one stage brackets it and takes
    the difference, so a budget that spans several pipeline phases is expressible
    without production code having to know where the phase boundaries are.
    """
    with _parse_tally_lock:
        return _parse_tally


def parse_html_tree(html: str | None):
    """Parse HTML once with lxml and return the root element.

    All discovery passes (anchors, frames, forms) and the XPath selector path
    reuse this single parse so we allocate and walk the tree only once instead
    of running three separate :class:`HTMLParser` subclasses.

    ``lxml.etree.HTML`` (rather than ``lxml.html.fromstring``) is used because
    it wraps fragments in a document tree and preserves legacy ``<frameset>`` /
    ``<frame>`` markup that the lenient ``fromstring`` path otherwise drops.
    Returns ``None`` for empty/invalid input.
    """
    global _parse_tally
    with _parse_tally_lock:
        _parse_tally += 1
    if not html:
        return None
    try:
        return etree.HTML(html)
    except (ParserError, ValueError, TypeError):
        return None


def _diagnostic_tree(html: str | None, tree: Any | None) -> Any | None:
    return tree if tree is not None else (parse_html_tree(html) if html else None)


def _diagnostic_inputs(html: str | None, tree: Any | None) -> Any | None:
    return _diagnostic_tree(html, tree) if html else None


@dataclass(frozen=True)
class _Walkable:
    """A source URL and the tree to walk, both known good."""

    source_url: NormalizedUrl
    tree: Any


def _walkable(html: str | None, source_url: NormalizedUrl | None, tree: Any | None) -> _Walkable | None:
    """What a diagnostic extractor needs before it can look at anything, or ``None``.

    Three facts, and each one is a precondition the extractors all share: there is
    a source URL to attribute a finding to, the HTML is parsed, and the parse
    produced a tree. Stated per extractor it reads as three separate guards, and
    it is the ``tree is None`` one that gets dropped -- a diagnostic that walks
    ``None`` reports nothing, which is a passing test and a permanently dead
    signal.

    A record rather than the bare tree so the caller gets the *narrowed* URL back.
    Handing back the tree alone leaves every caller's ``source_url`` typed
    ``str | None`` even though the None case has just returned, which is how a
    chokepoint ends up pushing its own precondition back onto the call sites.
    """
    if not source_url:
        return None
    parsed = _diagnostic_inputs(html, tree)
    return None if parsed is None else _Walkable(source_url=source_url, tree=parsed)


class _FrameCollector:
    """Back-compat collector: parse once with lxml and collect frame/iframe srcs."""

    def __init__(self, source_url: NormalizedUrl) -> None:
        self.source_url = source_url
        self.frames: list[dict[str, str]] = []

    def feed(self, html: str) -> None:
        tree = _diagnostic_tree(html, None)
        if tree is None:
            return
        for el in tree.iter("frame", "iframe"):
            src = (el.get("src") or "").strip()
            normalized = self.source_url.resolve(src)
            if normalized is None:
                continue
            self.frames.append(
                {
                    "url": normalized.raw,
                    "name": el.get("name") or "",
                    "title": el.get("title") or "",
                }
            )


def frames_from(
    walkable: _Walkable,
    source_url: NormalizedUrl | None,
) -> list[dict[str, Any]]:
    """Frame/iframe diagnostics, admitted through the source URL's own policy.

    ``resolve`` on the source value is the filter: a frame the job's policy
    refuses is dropped here, and there is no second policy parameter to pass
    a different answer to.
    """
    source_host = walkable.source_url.host
    frames: list[dict[str, Any]] = []
    seen: set[NormalizedUrl] = set()
    for el in walkable.tree.iter("frame", "iframe"):
        src = (el.get("src") or "").strip()
        if not src:
            continue
        normalized_url = walkable.source_url.resolve(src)
        if normalized_url is None:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        frame_host = normalized_url.host
        frames.append(
            {
                "url": normalized_url.raw,
                "normalized_url": normalized_url.raw,
                "name": el.get("name") or "",
                "title": el.get("title") or "",
                "source_url": source_url.raw if source_url is not None else None,
                "host": frame_host,
                "is_same_host": bool(frame_host) and frame_host == source_host,
            }
        )
    return frames


_CONTROL_TAGS = ("input", "select", "textarea", "button")


def forms_from(
    walkable: _Walkable,
) -> list[dict[str, Any]]:
    forms: list[dict[str, Any]] = []
    for form_el in walkable.tree.iter("form"):
        attrs = {key.lower(): value for key, value in form_el.attrib.items()}
        action = unescape(str(attrs.get("action", "")).strip())
        normalized_action = walkable.source_url.resolve(action or walkable.source_url.raw)
        if normalized_action is None:
            continue
        controls: list[dict[str, str]] = []
        for control in form_el.iter(*_CONTROL_TAGS):
            cattrs = {key.lower(): value for key, value in control.attrib.items()}
            tag = str(control.tag)
            control_type = (
                (cattrs.get("type") or "text").lower() if tag == "input" else cattrs.get("type", "")
            )
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
                "action": normalized_action.raw,
                "normalized_action": normalized_action.raw,
                "target": attrs.get("target", ""),
                "name": attrs.get("name", ""),
                "id": attrs.get("id", ""),
                "controls": controls,
            }
        )
    return forms


def html_diagnostics(
    html: str | None,
    source_url: NormalizedUrl | None,
    *,
    tree: Any | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    # One parse, one guard. The two extractors used to take the raw html and
    # each re-derive the walkable, which put the "is there a tree to walk"
    # precondition -- the one that silently yields an empty diagnostic rather
    # than an error -- in front of both of them, spelled twice.
    walkable = _walkable(html, source_url, tree)
    if walkable is None:
        return diagnostics
    frames = frames_from(walkable, source_url)
    forms = forms_from(walkable)
    if frames:
        diagnostics["frames"] = frames
    if forms:
        diagnostics["forms"] = forms
    return diagnostics


def extract_discovered_links(
    html: str | None,
    source_url: NormalizedUrl | None,
    *,
    max_links: int = 250,
    tree: Any | None = None,
) -> tuple[list[Any], int, bool]:
    if not html or not source_url:
        return [], 0, False
    source_host = source_url.host
    if tree is None:
        tree = parse_html_tree(html)
    if tree is None:
        return [], 0, False
    # Single pass: normalize/dedupe inline instead of materializing an
    # intermediate per-anchor dict list (memory on link-heavy pages).
    links: list[Any] = []
    seen: set[NormalizedUrl] = set()
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
        normalized_url = source_url.resolve(href)
        if normalized_url is None or normalized_url in seen:
            continue
        seen.add(normalized_url)
        total_count += 1
        if len(links) >= max_links:
            continue
        text = re.sub(r"\s+", " ", unescape("".join(el.itertext()))).strip()
        host = normalized_url.host
        links.append(
            DiscoveredLink(
                url=href,
                normalized_url=normalized_url,
                text=text,
                rel=str(attrs.get("rel", "")).split(),
                source_url=source_url,
                host=host,
                is_same_host=host.lower() == source_host,
            )
        )
    return links, total_count, total_count > len(links)


def normalized_extraction(extracted: ExtractedContent) -> tuple[str, str | None]:
    """The ``(text, markdown)`` an extractor result contributes to a crawl result.

    Every caller of every extractor normalized the result the same two-line way
    -- ``extracted.text or ""`` and ``extracted.markdown`` -- so the
    normalization had as many copies as there were extractor call sites. The
    ``or ""`` is load-bearing: it is what keeps an extractor's ``None`` text
    from reaching a result at all, and a call site that omitted it produced a
    None that only surfaced further downstream.

    It lives here, beside ``ExtractedContent``, because that is the type whose
    invariant it is: the coercion is a fact about extractor output, not about
    any one strategy's result builder.
    """
    return extracted.text or "", extracted.markdown


def extract_post_fetch_discovery(
    html: str | None,
    final_url: NormalizedUrl | None,
    *,
    js_base_url: NormalizedUrl | None = None,
    max_links: int = 250,
    include_diagnostics: bool = False,
    include_links: bool = False,
    include_js_endpoints: bool = False,
    include_metadata: bool = False,
    host_policy: HostPolicy,
) -> dict[str, Any]:
    """Sync post-fetch discovery block for the strategy result builders.

    Parses the HTML once and derives frames/forms diagnostics, discovered
    links, raw JS endpoints, and the pagination hint in one pass. Pure CPU
    work with no I/O: both builders run it via ``asyncio.to_thread`` so page
    parsing never parks the event loop. Returns only plain data (dicts,
    lists, strings, DiscoveredLink values) -- the lxml tree never escapes.
    The pagination hint is egress-filtered HERE rather than at the call sites:
    this block used to promise each builder would keep its own filter
    contract, and the browser builder silently dropped it, so a
    ``javascript:`` rel=next escaped into the response. Filtering at the
    producer covers every consumer.
    """
    tree = parse_html_tree(html) if html and (include_diagnostics or include_links) else None
    diagnostics = (
        html_diagnostics(html, final_url, tree=tree) if include_diagnostics else {}
    )
    if include_links:
        discovered_links, discovered_link_count, discovered_links_truncated = extract_discovered_links(
            html, final_url, max_links=max_links, tree=tree
        )
    else:
        discovered_links, discovered_link_count, discovered_links_truncated = [], 0, False
    js_endpoints = (
        extract_js_endpoints(
            html,
            js_base_url if js_base_url is not None else final_url,
            host_policy=host_policy,
        )
        if include_js_endpoints
        else []
    )
    # The page metadata rides the tree that was parsed for THIS call's
    # diagnostics/links -- never a parse of its own. An empty dict means
    # "no tree existed", and shaping falls back to its own single parse;
    # a non-empty dict is authoritative and crosses the persistence
    # boundary as plain JSON (the tree itself never leaves this function).
    llm_metadata: ExtractedPageMetadata = {}
    if include_metadata and tree is not None and final_url:
        llm_metadata = _llm_metadata_from_tree(tree, final_url)
    return {
        "diagnostics": diagnostics,
        "discovered_links": discovered_links,
        "discovered_link_count": discovered_link_count,
        "discovered_links_truncated": discovered_links_truncated,
        "js_endpoints": js_endpoints,
        "next_page_url": detect_pagination(html, final_url),
        "llm_metadata": llm_metadata,
    }


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


def extract_js_endpoints(
    html: str | None,
    source_url: NormalizedUrl | None = None,
    *,
    host_policy: HostPolicy,
) -> list[dict[str, str]]:
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
            if source_url is None:
                # No base to resolve against: keep the raw scrape. The
                # absolute-only filter below still admits or drops it.
                resolved = endpoint
            else:
                admitted = source_url.resolve(endpoint)
                # A refused endpoint can never survive the egress filter
                # downstream; dropping it here keeps raw text out of the list.
                if admitted is None:
                    continue
                resolved = admitted.raw
            if resolved in seen:
                continue
            seen.add(resolved)
            endpoints.append({"url": resolved})
    if source_url is None:
        return [
            endpoint
            for endpoint in endpoints
            if not endpoint["url"].lower().startswith(("http://", "https://"))
            or admit_or_none(endpoint["url"], policy=host_policy) is not None
        ]
    return filter_js_endpoints(endpoints, resolve_against=source_url, policy=host_policy)


_NEXT_REL_ANCHOR_RE = re.compile(r"<a\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_NEXT_REL_LINK_RE = re.compile(r"<link\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def detect_pagination(
    html: str | None,
    final_url: NormalizedUrl | None,
) -> NormalizedUrl | None:
    """Auto-detect the next-page URL via ``rel="next"`` hints.

    Checks both ``<a rel="next" href="...">`` and
    ``<link rel="next" href="...">`` and resolves the href against the
    final page URL. Returns ``None`` when no pagination hint is present.

    The href is attacker-controlled HTML, so the resolved URL leaves the
    service through the egress filter (the value's own ``resolve``) HERE rather
    than at each call site: a non-http(s) target (``javascript:``, ``data:``)
    becomes ``None``, and a consumer that forgot to filter cannot leak it.
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
                return final_url.resolve(href_match.group(1).strip())
    return None
