"""Every name in ``app/`` reached privately, declared with why it may stay that way.

Two kinds of private reach are recorded here, and they are not equally bad.

**A test reaching into ``app``** is the visible cost of a module having no level
above its insides. 43 of the 49 structural commits that moved ``app/`` also moved
``tests/``, and that ratio is what this file exists to shrink.

**A module reaching into a sibling** is worse, because it means the module
boundary is a convention nobody is holding. Six modules do it, thirteen reaches
in all: ``app/crawl/engine.py`` reaches into ``app/crawl/robots.py`` for four
private classes and two pattern helpers, and ``app/crawl/strategies/http.py``
reaches into ``content_cache_key`` and ``request_profile``. Nothing checked that,
and nothing stopped it growing.

Both are gated the same blunt way: **the name appears here, with a kind and a
reason, or the run is red.** A gate that weighs the merit of a reach is a
convention, and conventions decay. What the bluntness buys is that a new weld
cannot land without somebody writing down why it is acceptable.

One table, not two, because a name can be reached from both sides -- and
``app.crawl.strategies.http_client_pool._drain_pending_client_closes`` is, by a
test and by two production modules -- and one name is one declaration whichever
hand reaches for it. The ``side`` field records who reaches, not what they are
allowed to.

Kinds, and what each one claims:

``harness lifecycle``
    Process or executor teardown the caller must drive itself, because it owns the
    thing being torn down. Real: a harness must stop the world it started.

``chokepoint state``
    A cap, a cache, a lock, a counter. Not a missing seam -- a constant is private
    because nobody outside the module may change it, and promoting one would be
    the alias this file exists to prevent: public and still unwelded. The check
    that a name really is state and not a hole: it appears in a public default, a
    public return type, or nowhere at all. A constant a caller is expected to
    *set* is not state, it is a missing seam.

``private type``
    A private class, or a private method on a public one. A private method is an
    implementation step by definition: the class is public and the method is not.

``implementation step``
    A step inside an algorithm rather than a contract. A test reaching one is
    asserting a mechanism; the fix is to rewrite the test to drive the contract
    above, not to promote the step.

``unreviewed``
    Recorded from the tree when this file was created. No one has argued for it
    yet, and these are the debt.

The names are verified against both trees in both directions on every run, so the
*names* cannot drift from reality and only the reasons are hand-maintained. That
guard is what stops this becoming another hand-kept mirror of generated truth.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

Kind = Literal[
    "unreviewed",
    "harness lifecycle",
    "chokepoint state",
    "private type",
    "implementation step",
]
Side = Literal["test", "app"]


class Declaration(NamedTuple):
    """One private reach: why it may stay, and which hand reaches for it."""

    kind: Kind
    reason: str
    side: tuple[Side, ...] = ()


def _d(kind: Kind, reason: str, side: tuple[Side, ...] = ()) -> Declaration:
    return Declaration(kind, reason, side)


_CHOKEPOINT_STATE = _d(
    "chokepoint state",
    "Module-private data -- a cap, a cache, a lock, a counter. Not a missing seam.\n"
    "A constant is private because nobody outside the module may change it, and promoting\n"
    "one would be exactly the alias this file exists to prevent: the name would be public\n"
    "and still unwelded. A caller reading a configured cap is white-box by nature and fine;\n"
    "what would not be fine is the caller being able to write it.\n"
    "\n"
    "The check that this is really state and not a hole: the name appears in a public\n"
    "default, a public return type, or nowhere at all. A constant a caller is expected to\n"
    "*set* is not chokepoint state, it is a missing seam, and it belongs in the public\n"
    "surface instead.",
)

_IMPLEMENTATION_STEP = _d(
    "implementation step",
    "A step inside an algorithm rather than a contract. A test reaching one is asserting a\n"
    "mechanism, and the fix is to rewrite the test to drive the contract above and delete\n"
    "this row -- not to promote the step, which would make the mechanism part of the\n"
    "module's promise.",
)

_LIFECYCLE_EXTRACTION_EXECUTOR = _d(
    "harness lifecycle",
    "The suite starts the doc-extraction pool through this module, so only this\n"
    "module can stop it. conftest teardown is the one caller that legitimately reaches\n"
    "inside, because it owns the thing being torn down.\n",
)

_LIFECYCLE_SHUTDOWN_DOC_EXTRACTION_EXECUTOR = _d(
    "harness lifecycle",
    "The counterpart to the executor above, for the same reason: session teardown\n"
    "cannot ask a public seam to stop a pool it did not create.\n",
)

_LIFECYCLE_CONFIGURE_DOC_WORKER_LOGGING = _d(
    "harness lifecycle",
    "Configures logging inside spawned doc workers. Asserted directly because the\n"
    "configuration is the contract and it is not reachable from a return value.\n",
)

_LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES = _d(
    "harness lifecycle",
    "Draining superseded client closes is shutdown work, and shutdown work is owned by\n"
    "the module holding the pending set. It stays private because each caller is\n"
    "draining a pool it did not create -- the same argument the conftest rows make,\n"
    "with the difference that these are production callers. So if a third appears the\n"
    "answer is a public drain entry point, not a third reach.\n",
)

_PROCESS_WIDE_JAR = _d(
    "chokepoint state",
    "A process-wide singleton, reached by request_profile and main. Declared only\n"
    "until #75 decides whether cookie-jar ownership moves: if two modules are\n"
    "entitled to the same jar then the jar is a shared resource with no public\n"
    "accessor, which is a hole rather than a private name.\n",
)


_PRIVATE_TYPE = _d(
    "private type",
    "A private class, or a private method on a public one. A private *method* is an\n"
    "implementation step by definition: the class is public and the method is not. A private\n"
    "*class* is a judgement -- it stays private if nothing constructs it from outside, and\n"
    "becomes public if it has a lifecycle the caller owns, as `_ResultSpill` does.",
)

# Every private reach, keyed `module.name` across both trees.
DECLARED: dict[str, Declaration] = {
    "app.api.async_crawl_runner._async_crawl_semaphore": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.api.async_crawl_runner._async_crawl_semaphore_loop": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.api.async_crawl_runner._get_async_crawl_semaphore": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.job_runner._job_engine_payload": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.job_runner._run_job": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.job_runner._tasks": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.api.main_helpers._build_job_status": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.render._render_options": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.response_shaping._aunified_response_from_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.response_shaping._failed_unified_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.response_shaping._shape_all": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.webhooks._max_webhook_size": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.cgroup_sense._read_int": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.classify._strip_html_noise": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.admission._fetch_and_decode": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.admission._frontier_identity": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.content_cache.ContentCache._remember_memory_locked": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.decompression._DECODABLE_ENCODINGS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.decompression._decompress_bounded": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._collect_document_item_lines": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._document_item_range": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._format_extracted_document_text": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._is_document_item_marker": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._is_document_non_item_section": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._join_document_paragraph_lines": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._looks_like_document_heading": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._next_non_empty_document_line": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.document_text._should_join_document_paragraph": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.engine._LAST_SPILL_GC_MONOTONIC": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.engine._ResultSpill": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.crawl.engine._ResultSpill._gc_stale_spills": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.crawl.engine._RobotsPathMatcher": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.engine._SPILL_GC_MIN_INTERVAL_SECONDS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.engine._SPILL_PREFIX": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.engine._get_shared_content_cache": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.engine._parse_lastmod": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.engine._payload_policy": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.engine._purge_robots_cache": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.engine._resource_gate": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.engine._robots_allows": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.engine._robots_pattern_to_regex": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.engine._seed_is_fresh": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.engine._stale_seeds": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.engine.robots_cache._entries": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.engine.robots_cache._lock": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.extraction._DOC_POOL": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.extraction._DocWorker": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.crawl.extraction._EXTRACTION_DOC_ACQUIRE_TIMEOUT": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.extraction._EXTRACTION_DOC_MAX_INPUT_BYTES": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.extraction._EXTRACTION_DOC_TIMEOUT": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.extraction._FrameCollector": _d(
        _IMPLEMENTATION_STEP.kind, _IMPLEMENTATION_STEP.reason, ["test"]
    ),
    "app.crawl.extraction._POOL_LOCK": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.extraction._acquire_doc_worker": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._configure_doc_worker_logging": _d(
        _LIFECYCLE_CONFIGURE_DOC_WORKER_LOGGING.kind, _LIFECYCLE_CONFIGURE_DOC_WORKER_LOGGING.reason, ["test"]
    ),
    "app.crawl.extraction._doc_pool_health": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._doc_pool_pids": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._doc_task_hooks": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._parse_html_tree": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._reset_doc_pool": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._retire_doc_worker": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._spawn_doc_worker": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._terminate_doc_process": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.extraction._walkable": _d(_IMPLEMENTATION_STEP.kind, _IMPLEMENTATION_STEP.reason, ["test"]),
    "app.crawl.fetch_helpers._SITEMAP_CACHE": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.fetch_helpers._fetch_and_decode": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.fetch_helpers._fetch_decode_discovery": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.fetch_helpers._fetch_text": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.job_store._Expected": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.llm_format._extract_llm_metadata": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.payload_helpers._payload_policy": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.payload_helpers._payload_urls": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.policy._ERROR_CODE_BY_FIELD": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.recursive_admission._batch_is_http_heavy": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._CLASSIFICATION_BY_EXCEPTION": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.result_helpers._binary_no_text_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._classify_error_code": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._crawl_result_from_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._crawl_result_from_exception": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._engine_result_from_exception": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._failed_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._failed_result_for": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._make_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._skipped_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._terminal_crawl_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._terminal_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.robots._MatchAll": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.crawl.robots._PrefixMatcher": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.crawl.robots._RobotsPathMatcher": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.crawl.robots._RobotsPatternCache": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.crawl.robots._compile_robots_pattern": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.crawl.robots._robots_pattern_to_regex": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.crawl.robots.robots_cache._entries": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.strategies.browser.BrowserRenderer._extract_text": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.browser.BrowserRenderer._get_browser_kwargs": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.browser.BrowserRenderer._harvest_timeout_for": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.browser.BrowserRenderer._make_response_handler": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.browser.BrowserRenderer._scroll_config_for": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.browser._BROWSER_COOKIES_BY_DOMAIN": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.browser._BROWSER_COOKIES_BY_DOMAIN._lock": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.browser._BROWSER_COOKIES_MAX": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.browser._assert_safe_final_url": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.browser._is_ssrf_blocked": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.browser._make_ssrf_route_handler": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.browser._make_ssrf_websocket_handler": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.browser._resolve_browser_target": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.browser._store_browser_cookies": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.browser_actions.BrowserActionsMixin._dispatch_action": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.browser_actions._MAX_ACTION_WAIT_SECONDS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.browser_pool._derive_browser_pool_size": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.content_cache_key._content_cache_key": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test", "app"],
    ),
    "app.crawl.strategies.content_cache_key._effective_max_content_length": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test", "app"],
    ),
    "app.crawl.strategies.cookie_jar._COOKIE_JAR_MAX_HOSTS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.cookie_jar._COOKIE_NAMESPACE": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.cookie_jar._GLOBAL_COOKIE_JAR": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test", "app"]
    ),
    "app.crawl.strategies.cookie_jar._GLOBAL_COOKIE_JAR._store": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.document._default_document_text_extractor": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.document._extract_pdf_bytes_to_markdown": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.document._format_table": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.document._pdf_to_markdown": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.fetch_policy._RETRY_BACKOFF_CAP_SECONDS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.fetchers._cffi_fetch": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.fetchers._default_http_fetcher": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http._http_strategy_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http_client_pool._DEFAULT_HTTP_CLIENT": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.http_client_pool._LoopBoundPooledClient": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.http_client_pool._NoStoreCookieJar": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.http_client_pool._PENDING_CLIENT_CLOSE": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.http_client_pool._PENDING_CLIENT_CLOSE_LOCK": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.http_client_pool._PENDING_CLIENT_CLOSE_MAX": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.http_client_pool._build_async_client": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http_client_pool._build_cffi_session": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http_client_pool._default_settings": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http_client_pool._drain_pending_client_closes": _d(
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.kind,
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.reason,
        ["test", "app"],
    ),
    "app.crawl.strategies.http_client_pool._queue_superseded_close": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http_client_pool._release_dropped_client": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http_client_pool._safe_aclose": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.http_client_pool._schedule_client_close": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.pinned_transport._CREDENTIAL_HEADERS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.pinned_transport._DNS_CACHE": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.pinned_transport._DNS_CACHE._lock": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.pinned_transport._MAX_REDIRECT_HOPS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.pinned_transport._build_pinned_request": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.pinned_transport._build_pinned_request_async": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.strategies.pinned_transport._declared_length_exceeds": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test", "app"],
    ),
    "app.crawl.strategies.pinned_transport._http_413": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["app"],
    ),
    "app.crawl.strategies.pinned_transport._is_private_or_loopback_ip": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.pinned_transport._redirect_target": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.pinned_transport._resolve_ips": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.request_profile._apply_credentials": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.request_profile._cookie_header_for_url": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.request_profile._headers_for_strategy": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test", "app"],
    ),
    "app.crawl.strategies.request_profile._select_fingerprint": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.request_profile._store_set_cookies": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategy_chain._starting_strategies": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.logging_config._BC_OWNED_HANDLER": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.main._warn_if_bound_off_loopback": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.mcp.tools._handle_crawl": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.mcp.tools._handle_map": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.mcp.tools._handle_render": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.mcp.tools._handle_search": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.models._LITERAL_FIELDS": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.models._LiteralChecked": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.models._literal_members": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.output_formats._csv_neutralize": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.output_formats._default": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.output_formats._safe": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.resource_pressure._BROWSER_RAM_FRACTION": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._BROWSER_TASK_RAM_BYTES": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._DOC_TASK_RAM_BYTES": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._HTTP_TASK_RAM_BYTES": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._MAX_DERIVED_POOL_SIZE": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._build_admission_policy": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.scheduler.DomainScheduler._purge_expired": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.scheduler._COOLDOWN_PURGE_INTERVAL": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.scheduler._MAX_COOLDOWN_WAIT": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.scheduler._STATUS_ACCOUNTING": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.scheduler._TokenBucket": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.search.providers._MAX_TEXT_LENGTH": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.search.providers._collect_results": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.search.providers._provider_http": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.settings._default_http_keepalive": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.settings._default_http_max_connections": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.validation._PSL": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.validation._host_is_public": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.validation._is_public_ip_value": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.validation._reset_guard_memo": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
}
