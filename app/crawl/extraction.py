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
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from html import unescape
from typing import Any, Literal, NamedTuple, TypeVar
from urllib.parse import urljoin, urlparse

from lxml import etree
from lxml.etree import ParserError
from markdownify import markdownify as html_to_md
from markitdown import MarkItDown
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
from app.crawl.job_store import durable_page_identity
from app.crawl.llm_format import _llm_metadata_from_tree, _text_from_html
from app.models import DiscoveredLink
from app.resource_pressure import doc_worker_count
from app.validation import (
    HostPolicy,
    filter_js_endpoints,
    get_env_float,
    get_env_int,
    parse_bool_env,
    public_url_or_none,
    safe_public_url_or_none,
)

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
_EXTRACTION_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="extraction")

# Default 2 keeps memory bounded: each worker loads markitdown + parsers and the
# production container runs with a 3g mem limit (compose pins `mem_limit: 3g`).
# Raise via CRAWLUTRON_EXTRACTION_DOC_WORKERS when more PDF/Office
# throughput is needed and the memory budget allows it.
_DEFAULT_DOC_WORKERS = 2


# The count lives in app.resource_pressure (the module admission reserves RAM
# in) so the spawned pool can never drift from the reservation. Kept as a thin
# delegation for call-site readability inside this module.
def _doc_extraction_worker_count() -> int:
    """Configured concurrency for markitdown extraction (process pool size)."""
    return doc_worker_count()


# Document extraction (markitdown) runs in a PERSISTENT pool of worker
# processes. Each worker imports markitdown once (it
# stays in the worker's sys.modules) and serves many documents, amortizing
# the spawn + import cost of the old spawn-per-document design. Timeout-kill
# semantics are kept per task: a worker that exceeds
# _EXTRACTION_DOC_TIMEOUT -- or dies mid-task -- is terminated and reaped
# immediately, and its slot is respawned on the next acquisition, so a
# pathological document can neither block the pool nor linger as a zombie.
# Sized by doc_worker_count() (app.resource_pressure -- the single home for the
# count) so memory stays within the container ceiling. Defined after the
# worker-count helper above.

# The start method is named, never inherited from the platform. The default is
# `fork` on Linux and `spawn` on macOS, and a forked extraction worker is a copy
# of the whole crawler: the running event loop, every open socket, the HTTP
# cache, and any lock another thread held at fork time -- held for good, since
# the thread that owned it does not exist in the child. This worker does nothing
# but CPU-bound markitdown over a pipe, so a clean interpreter is both safer and
# no more costly, and the persistent pool above already exists to amortize the
# spawn cost that naming it does not remove.
#
# It is also why the pool is honest about its own environment. A forked child
# freezes the parent's env at fork time, so a long-lived worker predating a
# setting never observed it -- which is how the timeout suite passed on macOS
# and failed on the same assertion on Linux, with no change to the code under
# test. Every process object handed to the child comes from this one context:
# a pipe from the default context and a process from another is not a pairing
# that works.
_DOC_WORKER_CONTEXT = multiprocessing.get_context("spawn")


class _DocWorker:
    """One persistent markitdown worker: process + parent end of its task pipe."""

    __slots__ = ("proc", "conn", "busy", "retired")

    def __init__(self, proc, conn) -> None:
        self.proc = proc
        self.conn = conn
        self.busy = False
        # True once the worker has been terminated. Termination is only
        # best-effort, so a retired process can still report ``is_alive()``;
        # "alive" is therefore not proof of "usable", and this flag -- not the
        # process state -- is what keeps a retired worker out of circulation.
        self.retired = False


_POOL_LOCK = threading.Condition(threading.Lock())
_DOC_POOL: list[_DocWorker] | None = None


def _test_hooks_enabled() -> bool:
    """Single gate for the TEST_MARKITDOWN_* child-process hooks.

    The hooks exist so tests can simulate child death/hang/output without
    real parsers, but a stray env var must never alter production behavior:
    they only fire when the operator explicitly exports
    CRAWLUTRON_TEST_HOOKS=1 alongside the per-hook flag.
    """
    return parse_bool_env("CRAWLUTRON_TEST_HOOKS", default=False)


def _doc_task_hooks() -> dict | None:
    """Snapshot the hermetic TEST_MARKITDOWN_* seam for one task (parent-side).

    Workers are persistent: they would otherwise observe the env from their
    spawn moment, not the caller's current test flags. The parent (which
    always sees current env) snapshots the gated hooks per call and ships
    them with the task; the worker honors the message, never its own env.
    Returns None in production (gate off) so a stray test flag can never
    alter real behavior.
    """
    if not _test_hooks_enabled():
        return None
    return {
        "die": parse_bool_env("TEST_MARKITDOWN_DIE", default=False),
        "hang": parse_bool_env("TEST_MARKITDOWN_HANG", default=False),
        "output": os.environ.get("TEST_MARKITDOWN_OUTPUT"),
    }


def _configure_doc_worker_logging() -> None:
    """Quiet expected parser chatter in the document worker.

    The worker is a bare child process with no service logging, so without this
    pdfminer logs a WARNING per font whose descriptor lacks a usable FontBBox
    (common on real-world municipal PDFs). The text that parsed still extracts,
    so the warning carries no signal; drop it at the source and keep ERROR+
    visible.
    """
    logging.getLogger("pdfminer").setLevel(logging.ERROR)


def _doc_worker_main(conn) -> None:
    """Persistent worker entry: serve markitdown tasks until killed or orphaned.

    Each task arrives as ``(body, hooks)``; hooks come from the parent's
    per-call snapshot (never the worker's spawn-time env). A ``die`` hook
    exits without sending (a worker segfaulting on hostile input).
    """
    try:
        _configure_doc_worker_logging()
        while True:
            try:
                body, hooks = conn.recv()
            except (EOFError, OSError):
                return
            if hooks is not None:
                if hooks.get("die"):
                    with suppress(Exception):
                        conn.close()
                    os._exit(1)
                if hooks.get("hang"):
                    time.sleep(10)  # Longer than any test timeout
                if hooks.get("output") is not None:
                    with suppress(Exception):
                        conn.send(("ok", hooks["output"]))
                    continue
            try:
                md = _markitdown_extract(body)
                conn.send(("ok", md))
            except Exception:
                with suppress(Exception):
                    conn.send(("err", None))
    finally:
        with suppress(Exception):
            conn.close()


# Grace period for a document worker to honor SIGTERM before we escalate.
_DOC_TERMINATE_GRACE_SECONDS = 5.0


def _terminate_doc_process(proc) -> None:
    """Terminate a runaway document-extraction process and reap it.

    SIGTERM is not sufficient on its own: a worker wedged in an
    uninterruptible native conversion (or one whose library installed a signal
    handler) absorbs it and keeps running, holding its pipe, its memory and its
    pool slot for the life of the service. Escalate to SIGKILL, and join after
    the kill so the reaped child cannot linger as a zombie.
    """
    with suppress(Exception):
        proc.terminate()
    with suppress(Exception):
        proc.join(timeout=_DOC_TERMINATE_GRACE_SECONDS)
    with suppress(Exception):
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=_DOC_TERMINATE_GRACE_SECONDS)


def _markitdown_extract(body: bytes) -> str | None:
    """Convert document bytes with markitdown (worker-side real path only).

    Test hooks are NOT read here: the parent snapshots them per task and the
    worker honors the shipped message, so persistent workers never observe
    stale env.
    """
    res = MarkItDown().convert_stream(io.BytesIO(body))
    return res.text_content if res is not None else None


def _spawn_doc_worker() -> _DocWorker:
    """Start one persistent worker and return its parent-side handle."""
    parent_conn, child_conn = _DOC_WORKER_CONTEXT.Pipe(duplex=True)
    proc = _DOC_WORKER_CONTEXT.Process(target=_doc_worker_main, args=(child_conn,), daemon=True)
    proc.start()
    child_conn.close()  # parent keeps only its end of the pipe
    return _DocWorker(proc, parent_conn)


def _retire_doc_worker(worker: _DocWorker) -> None:
    """Terminate and reap one worker, leaving its (dead) slot for respawn.

    Respawn is deliberately lazy (next acquisition): the bounds suite
    requires zero live children immediately after a timeout/child-death,
    and a dead-idle slot costs nothing until the next document arrives.

    The retired mark is set first, before anything slow, because retirement is
    not always performed under ``_POOL_LOCK``: without it, the lazy window
    between "terminated" and "respawned" would let an acquisition hand a doomed
    worker straight back to the next caller.
    """
    worker.retired = True
    _terminate_doc_process(worker.proc)
    with suppress(Exception):
        worker.conn.close()


def _teardown_doc_pool_locked() -> None:
    """Terminate every pooled worker. Caller must hold ``_POOL_LOCK``."""
    global _DOC_POOL
    pool, _DOC_POOL = _DOC_POOL, None
    for worker in pool or []:
        _retire_doc_worker(worker)


def _document_extraction_enabled() -> bool:
    return parse_bool_env("CRAWLUTRON_EXTRACTION_DOC_ENABLED", default=True)


def _acquire_doc_worker() -> _DocWorker | None:
    """Take an idle pool worker, spawning on demand up to the configured size.

    The pool starts empty and grows only as concurrent demand requires: a
    test doing one extraction owns exactly one worker, so the historical
    zero-live-children-after-death invariant still holds. Dead idle slots
    (retired after a timeout/child-death) are respawned in place here, so
    normal acquisitions never observe them. Retired slots are never handed
    out, alive-report or not.

    Returns ``None`` when no worker became available within
    ``_EXTRACTION_DOC_ACQUIRE_TIMEOUT``. Callers must treat that as "document
    extraction unavailable for this document" (the same degradation as a
    per-task timeout): the alternative used to be waiting with no deadline, so
    one busy flag that its holder never cleared -- an exception escaping
    between acquisition and the releasing ``finally`` -- wedged every later
    document extraction in the process.
    """
    global _DOC_POOL
    if not _document_extraction_enabled():
        return None
    # Dead slots discovered here are replaced under the lock but TERMINATED
    # after it: the slot swap is instant, while the kill-escalation a
    # retirement runs can take ~10s (SIGTERM -> join(5) -> SIGKILL -> join(5)).
    # Running that under _POOL_LOCK would block every other acquisition and
    # every release for the whole window. A replaced slot is unreachable from
    # the pool the instant it is swapped out, so deferring only its teardown is
    # safe; the list is drained in the finally below, before this returns.
    dead_slots: list[_DocWorker] = []
    try:
        with _POOL_LOCK:
            if _DOC_POOL is None:
                _DOC_POOL = []
            # Reap retired slots before the scans. A retired slot has been
            # terminated and can never be handed out again (the handout scan
            # requires ``not retired``), so it must not keep occupying a pool
            # entry. The respawn scan below only replaces a slot whose process
            # is dead, which leaves a retired-but-still-``is_alive()`` slot
            # (SIGKILL could not reap it -- pathological) skipped by BOTH scans
            # and stranded in the pool forever: it held a PID while the size
            # budget grew around it, and ``len(_DOC_POOL)`` drifted permanently
            # above the usable count. Removing every retired slot here keeps the
            # pool and the budget consistent; the termination and the process
            # reap already ran when the slot was retired, so nothing live is
            # dropped. Re-running this on every loop iteration also catches a
            # slot retired by another thread while this acquirer waited.
            _DOC_POOL = [each for each in _DOC_POOL if not each.retired]
            want = _doc_extraction_worker_count()
            # The budget starts once the lock is actually held. A caller queued
            # behind a slow in-lock operation would otherwise pay that wait out
            # of a budget it never consumed, and could give up on a pool that
            # had a free slot the whole time it was blocked.
            deadline = time.monotonic() + _EXTRACTION_DOC_ACQUIRE_TIMEOUT
            while True:
                for worker in _DOC_POOL:
                    if not worker.busy and not worker.retired and worker.proc.is_alive():
                        worker.busy = True
                        return worker
                for index, worker in enumerate(_DOC_POOL):
                    if not worker.busy and not worker.proc.is_alive():
                        fresh = _spawn_doc_worker()
                        fresh.busy = True
                        _DOC_POOL[index] = fresh
                        dead_slots.append(worker)
                        return fresh
                # Retired slots were reaped above, so this is the live usable
                # set; when it is short of the configured size the pool grows by
                # one rather than degrading to "no worker" -- the failure mode
                # the reaped zombie slot used to cause (a slot nothing could
                # recover shrinking the pool permanently).
                usable = sum(1 for each in _DOC_POOL if not each.retired)
                if usable < want:
                    fresh = _spawn_doc_worker()
                    fresh.busy = True
                    _DOC_POOL.append(fresh)
                    return fresh
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "extraction_document_pool_unavailable",
                        extra={"slots": len(_DOC_POOL), "workers": want},
                    )
                    return None
                # Still a condition wait, not a poll loop: a release notifies and
                # wakes immediately. The bounded re-check exists only so a stuck
                # busy flag (nothing will ever notify) expires on its own.
                _POOL_LOCK.wait(timeout=min(remaining, _DOC_ACQUIRE_RECHECK_SECONDS))
    finally:
        # Teardown of any slot swapped out above, now that _POOL_LOCK is free.
        # ``_retire_doc_worker`` marks retired, terminates and reaps; this path
        # keeps the same retirement code as the timeout/child-death call sites.
        for worker in dead_slots:
            _retire_doc_worker(worker)


def _reset_doc_pool() -> None:
    """Tear down the pool so the next extraction rebuilds it (tests only)."""
    with _POOL_LOCK:
        _teardown_doc_pool_locked()


def _doc_pool_pids() -> list[int]:
    """Live worker PIDs (tests only: proves persistence, not spawn-per-task)."""
    with _POOL_LOCK:
        return [worker.proc.pid for worker in _DOC_POOL or [] if worker.proc.pid]


def _doc_pool_health() -> list[tuple[bool, bool, bool]]:
    """Per-worker ``(alive, reaped, busy)`` snapshot (tests only).

    The pool is persistent and sized above one, so idle workers legitimately
    stay alive across calls; the recoverable-failure contract is per worker,
    not process-global: a worker that died must be reaped (exitcode set, not a
    zombie) and no worker may be left marked busy mid-task.
    """
    with _POOL_LOCK:
        return [
            (worker.proc.is_alive(), worker.proc.exitcode is not None, worker.busy)
            for worker in _DOC_POOL or []
        ]


async def _shutdown_doc_extraction_executor() -> None:
    """Terminate persistent document-extraction workers (lifespan/test teardown)."""
    with _POOL_LOCK:
        _teardown_doc_pool_locked()


# Wall-clock bound (seconds) for a single CSS/XPath selector evaluation.
# A pathological selector (or a hostile payload) could otherwise run
# unbounded and tie up a request worker. Override via env var.
_EXTRACTION_SELECTOR_TIMEOUT = get_env_float("CRAWLUTRON_EXTRACTION_SELECTOR_TIMEOUT", 2.0)

# Wall-clock bound (seconds) for document extraction (markitdown).
# Override via CRAWLUTRON_EXTRACTION_DOC_TIMEOUT.
_EXTRACTION_DOC_TIMEOUT = get_env_float("CRAWLUTRON_EXTRACTION_DOC_TIMEOUT", 10.0)

# How long a caller waits for a free worker slot. A healthy pool releases a
# worker when its own task hits _EXTRACTION_DOC_TIMEOUT, so a full round of the
# pool is already generous; this bound exists for the unhealthy case, where a
# busy flag is never cleared and an unbounded wait would take the whole pool
# down with it. Override via CRAWLUTRON_EXTRACTION_DOC_ACQUIRE_TIMEOUT.
_EXTRACTION_DOC_ACQUIRE_TIMEOUT = get_env_float("CRAWLUTRON_EXTRACTION_DOC_ACQUIRE_TIMEOUT", 90.0)

# Re-check interval for a waiting acquirer (the escape hatch for "nothing will
# ever notify"); releases still wake waiters immediately via notify_all.
_DOC_ACQUIRE_RECHECK_SECONDS = 1.0

# #mem-guard: a markitdown worker process shares the container cgroup; an
# unbounded document can balloon it past the memory ceiling. Cap the input
# size and allow deployments to disable subprocess extraction entirely.
_EXTRACTION_DOC_MAX_INPUT_BYTES = get_env_int("CRAWLUTRON_EXTRACTION_DOC_MAX_BYTES", 20 * 1024 * 1024, lo=1)

# Maximum extracted markdown size (chars) to prevent unbounded output.
_MAX_EXTRACTED_MARKDOWN = 5 * 1024 * 1024  # 5 MB


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


def _eval_selector(fn: Callable[[], _SelectorResult], timeout: float) -> _SelectorResult | _SelectorTimedOut:
    """Run a selector evaluation in a worker thread so it cannot hang a request.

    Returns the callable's result, or ``_EXTRACTION_SELECTOR_SENTINEL`` if it
    exceeded ``timeout`` seconds.

    Uses a shared bounded thread pool to avoid thread leaks.
    """
    fut = _EXTRACTION_EXECUTOR.submit(fn)
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
    selectors: Selectors | None = None,
) -> ExtractedContent:
    """Extract ``(text, markdown)`` from raw content using the unified pipeline.

    The HTML branch hands the selector pair down rather than dispatching on it
    here: ``_extract_html`` already routes a selector to :func:`_extract_selected`
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
    # same thing. It was also unreadable as written: `elif not _is_html(...)` and
    # `if _is_html(...)` below are the same question asked in opposite polarity,
    # and nothing in the text said they had to agree. Now there is one answer and
    # two consumers, so a report and a dispatch cannot describe different bodies.
    is_html = _is_html(content_type, content)
    if selectors and not is_html:
        logger.warning(
            "extraction_selector_ignored_non_html",
            extra={
                "css_selector": selectors.css,
                "xpath": selectors.xpath,
                "content_type": content_type,
                "url": url,
            },
        )
    if is_html:
        return _extract_html(_decode(content), url, selectors)
    if _looks_like_document(content_type, content, url):
        body = (
            bytes(content)
            if isinstance(content, (bytes, bytearray, memoryview))
            else content.encode("utf-8", "replace")
        )
        return _extract_document(body, url)
    text = _decode(content).strip()
    return ExtractedContent(text or None, text or None)


def _html_to_markdown(html_fragment: str) -> tuple[str | None, str | None]:
    """Convert a raw HTML fragment to ``(text, markdown)`` for DOM targeting."""
    try:
        md = html_to_md(html_fragment) or None
    except Exception:
        md = None
    text = _text_from_html(html_fragment) or None
    return text, md


# The selector pair is ONE contract with three entry points: ``extract_content``
# (every strategy's funnel), ``_extract_html`` (the trafilatura-only path) and
# ``_extract_selected`` (the DOM-targeting engine). It used to be four spellings:
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


def _extract_selected(
    html: str,
    source_url: str | None,
    *,
    selectors: Selectors | None = None,
) -> ExtractedContent:
    """Mechanical DOM targeting: extract only the matched nodes as Markdown.

    CSS selectors are resolved with BeautifulSoup; XPath expressions with lxml.
    The matched subtree(s) are serialized and converted to Markdown, so callers
    can target a specific section (e.g. an agenda table) without Trafilatura's
    full-page boilerplate removal.
    """
    if not html or not selectors:
        return ExtractedContent(None, None)
    fragment: str | None = None
    css_selector = selectors.css
    if css_selector:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return ExtractedContent(None, None)
        soup = BeautifulSoup(html, "html.parser")
        selected = _eval_selector(lambda: soup.select(css_selector), _EXTRACTION_SELECTOR_TIMEOUT)
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
        tree = _parse_html_tree(html)
        if tree is None:
            return ExtractedContent(None, None)
        try:
            nodes = _eval_selector(lambda: tree.xpath(xpath), _EXTRACTION_SELECTOR_TIMEOUT)
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
    text, md = _html_to_markdown(fragment)
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
    ``_extract_html`` already falls through to markdownify and then plain
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


def _extract_html(
    html: str,
    url: str | None,
    selectors: Selectors | None = None,
) -> ExtractedContent:
    """The single owner of the HTML selector dispatch, for both callers.

    The unified :func:`extract_content` entry and the trafilatura-only strategy
    path both route HTML here, which is why the pair is a parameter on this
    function at all: one place decides whether a page is selector-targeted or
    full-page, so the two callers cannot disagree about the same page.
    """
    if selectors:
        return _extract_selected(html, url, selectors=selectors)
    try:
        with _suppress_trafilatura_chatter():
            md = _trafilatura_extract(
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
        md = _text_from_html(html) or None
    return ExtractedContent(md, md)


def _extract_document(body: bytes, url: str | None) -> ExtractedContent:
    """Extract markdown from document bytes with a wall-clock timeout.

    The markitdown conversion runs on a persistent pool worker with a hard
    per-task timeout so a pathological document cannot block the pool
    indefinitely; the worker is forcefully killed and its slot respawned.
    Output is also capped to prevent unbounded memory growth.
    """
    if not _document_extraction_enabled():
        return ExtractedContent(None, None)
    if len(body) > _EXTRACTION_DOC_MAX_INPUT_BYTES:
        logger.warning(
            "extraction_document_too_large",
            extra={"size_bytes": len(body), "max_bytes": _EXTRACTION_DOC_MAX_INPUT_BYTES},
        )
        return ExtractedContent(None, None)

    # Serve the document from the persistent pool with a hard wall-clock
    # timeout per task; on timeout -- or if the worker died mid-task -- the
    # runaway worker is forcefully terminated and reaped so it cannot linger
    # as a zombie, and its slot is respawned on the
    # next acquisition. Pool size bounds concurrent processes so memory
    # stays within the container ceiling.
    worker = _acquire_doc_worker()
    if worker is None:
        # No slot within the acquire budget: degrade this document like every
        # other document failure instead of wedging the request.
        return ExtractedContent(None, None)
    try:
        try:
            worker.conn.send((body, _doc_task_hooks()))
        except (EOFError, OSError):
            # Send raced a worker death: reap the slot and report empty.
            logger.warning("extraction_document_child_died")
            _retire_doc_worker(worker)
            return ExtractedContent(None, None)
        try:
            ready = worker.conn.poll(_EXTRACTION_DOC_TIMEOUT)
        except (EOFError, OSError):
            logger.warning("extraction_document_child_died")
            _retire_doc_worker(worker)
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
                _retire_doc_worker(worker)
                return ExtractedContent(None, None)
        else:
            logger.warning("extraction_document_timeout")
            _retire_doc_worker(worker)
            return ExtractedContent(None, None)
    finally:
        with _POOL_LOCK:
            # The worker may have been retired mid-task (timeout/child death).
            # Clearing busy is still correct: ``retired``, not ``busy``, is what
            # keeps a terminated worker out of circulation, and the notify lets
            # waiters reach the slot's lazy respawn instead of sitting on it.
            worker.busy = False
            _POOL_LOCK.notify_all()

    if not md or not md.strip():
        return ExtractedContent(None, None)

    # Cap output size.
    if len(md) > _MAX_EXTRACTED_MARKDOWN:
        md = md[:_MAX_EXTRACTED_MARKDOWN]
        logger.warning("extraction_document_truncated", extra={"max_chars": _MAX_EXTRACTED_MARKDOWN})
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
        return etree.HTML(html)
    except (ParserError, ValueError, TypeError):
        return None


def _diagnostic_tree(html: str | None, tree: Any | None) -> Any | None:
    return tree if tree is not None else (_parse_html_tree(html) if html else None)


def _diagnostic_inputs(html: str | None, tree: Any | None) -> Any | None:
    return _diagnostic_tree(html, tree) if html else None


@dataclass(frozen=True)
class _Walkable:
    """A source URL and the tree to walk, both known good."""

    source_url: str
    tree: Any


def _walkable(html: str | None, source_url: str | None, tree: Any | None) -> _Walkable | None:
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

    def __init__(self, source_url: str, *, host_policy: HostPolicy) -> None:
        self.source_url = source_url
        self.host_policy = host_policy
        self.frames: list[dict[str, str]] = []

    def feed(self, html: str) -> None:
        tree = _diagnostic_tree(html, None)
        if tree is None:
            return
        for el in tree.iter("frame", "iframe"):
            src = (el.get("src") or "").strip()
            normalized = public_url_or_none(
                src,
                resolve_against=self.source_url,
                policy=self.host_policy,
            )
            if normalized is None:
                continue
            self.frames.append(
                {
                    "url": normalized,
                    "name": el.get("name") or "",
                    "title": el.get("title") or "",
                }
            )


def _frames_from(
    walkable: _Walkable,
    source_url: str | None,
    host_policy: HostPolicy,
) -> list[dict[str, Any]]:
    source_host = (urlparse(walkable.source_url).hostname or "").lower()
    frames: list[dict[str, Any]] = []
    seen: set[str] = set()
    for el in walkable.tree.iter("frame", "iframe"):
        src = (el.get("src") or "").strip()
        if not src:
            continue
        normalized_url = public_url_or_none(
            urljoin(walkable.source_url, src),
            policy=host_policy,
        )
        if normalized_url is None:
            continue
        parsed = urlparse(normalized_url)
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        # Resolved once: hostname is Optional, and asking twice is how one field
        # ends up disagreeing with the other.
        frame_host = (parsed.hostname or "").lower()
        frames.append(
            {
                "url": normalized_url,
                "normalized_url": normalized_url,
                "name": el.get("name") or "",
                "title": el.get("title") or "",
                "source_url": source_url,
                "host": frame_host,
                "is_same_host": bool(frame_host) and frame_host == source_host,
            }
        )
    return frames


_CONTROL_TAGS = ("input", "select", "textarea", "button")


def _forms_from(
    walkable: _Walkable,
    host_policy: HostPolicy,
) -> list[dict[str, Any]]:
    forms: list[dict[str, Any]] = []
    for form_el in walkable.tree.iter("form"):
        attrs = {key.lower(): value for key, value in form_el.attrib.items()}
        action = unescape(str(attrs.get("action", "")).strip())
        normalized_action = public_url_or_none(
            urljoin(walkable.source_url, action) if action else walkable.source_url,
            policy=host_policy,
        )
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
                "action": normalized_action,
                "normalized_action": normalized_action,
                "target": attrs.get("target", ""),
                "name": attrs.get("name", ""),
                "id": attrs.get("id", ""),
                "controls": controls,
            }
        )
    return forms


def _html_diagnostics(
    html: str | None,
    source_url: str | None,
    *,
    host_policy: HostPolicy,
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
    frames = _frames_from(walkable, source_url, host_policy)
    forms = _forms_from(walkable, host_policy)
    if frames:
        diagnostics["frames"] = frames
    if forms:
        diagnostics["forms"] = forms
    return diagnostics


def _extract_discovered_links(
    html: str | None,
    source_url: str | None,
    *,
    host_policy: HostPolicy,
    max_links: int = 250,
    tree: Any | None = None,
) -> tuple[list[Any], int, bool]:
    if not html or not source_url:
        return [], 0, False
    norm_source = durable_page_identity(source_url, host_policy)
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
            normalized_url = safe_public_url_or_none(urljoin(source_url, href), policy=host_policy)
        except ValueError:
            continue
        if not normalized_url or normalized_url in seen:
            continue
        seen.add(normalized_url)
        total_count += 1
        if len(links) >= max_links:
            continue
        parsed = urlparse(normalized_url)
        text = re.sub(r"\s+", " ", unescape("".join(el.itertext()))).strip()
        # ``urlparse().hostname`` is None for schemes without a host
        # (``mailto:``, ``javascript:``). An unparseable host has no same-host
        # comparison: record it empty and let the caller filter, instead of
        # ``.lower()`` on a None that would drop the whole link batch.
        host = parsed.hostname or ""
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
    final_url: str | None,
    *,
    js_base_url: str | None = None,
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
    tree = _parse_html_tree(html) if html and (include_diagnostics or include_links) else None
    diagnostics = (
        _html_diagnostics(html, final_url, host_policy=host_policy, tree=tree) if include_diagnostics else {}
    )
    if include_links:
        discovered_links, discovered_link_count, discovered_links_truncated = _extract_discovered_links(
            html, final_url, host_policy=host_policy, max_links=max_links, tree=tree
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
    llm_metadata: dict[str, Any] = {}
    if include_metadata and tree is not None and final_url:
        llm_metadata = _llm_metadata_from_tree(tree, final_url)
    return {
        "diagnostics": diagnostics,
        "discovered_links": discovered_links,
        "discovered_link_count": discovered_link_count,
        "discovered_links_truncated": discovered_links_truncated,
        "js_endpoints": js_endpoints,
        "next_page_url": _detect_pagination(html, final_url, host_policy=host_policy),
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
    source_url: str | None = None,
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
            resolved = urljoin(source_url, endpoint) if source_url else endpoint
            if resolved in seen:
                continue
            seen.add(resolved)
            endpoints.append({"url": resolved})
    if source_url is None:
        return [
            endpoint
            for endpoint in endpoints
            if not endpoint["url"].lower().startswith(("http://", "https://"))
            or public_url_or_none(endpoint["url"], policy=host_policy) is not None
        ]
    return filter_js_endpoints(endpoints, resolve_against=source_url, policy=host_policy)


_NEXT_REL_ANCHOR_RE = re.compile(r"<a\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_NEXT_REL_LINK_RE = re.compile(r"<link\b[^>]*\brel=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _detect_pagination(
    html: str | None,
    final_url: str | None,
    *,
    host_policy: HostPolicy,
) -> str | None:
    """Auto-detect the next-page URL via ``rel="next"`` hints.

    Checks both ``<a rel="next" href="...">`` and
    ``<link rel="next" href="...">`` and resolves the href against the
    final page URL. Returns ``None`` when no pagination hint is present.

    The href is attacker-controlled HTML, so the resolved URL leaves the
    service through the egress filter (``public_url_or_none``) HERE rather
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
                return public_url_or_none(
                    href_match.group(1).strip(),
                    resolve_against=final_url,
                    policy=host_policy,
                )
    return None
