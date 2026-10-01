"""The suite's declared exceptions to "tests import public app names only".

A test reaching into an ``app`` module's private names is the visible cost of a
module having no level above its insides. 43 of the 49 structural commits that
moved ``app/`` also moved ``tests/``, and that ratio is the number this ledger is
meant to shrink.

The rule the gate enforces is deliberately blunt: **the suite imports no private
``app`` name unless that exact name appears here with a reason.** It is blunt
because a gate that weighs the merit of an import is a convention, and
conventions decay. What the bluntness buys is that a new private import cannot
land without someone writing down why it is acceptable.

Two kinds of entry, and the distinction is not cosmetic:

``UNREVIEWED``
    Recorded from the suite's current state when the ledger was created. No one
    has argued for it yet. These are the debt.

``DECLARED``
    Someone has looked at the name and written the reason. Two reasons exist
    that are not debt:

    *harness lifecycle* -- process or executor teardown a test harness must
    drive itself. ``tests/conftest.py`` starts the doc-extraction pool and has to
    stop it; no public seam can do that from outside the module that owns it.

    *implementation step* -- the name is a step inside an algorithm rather than a
    contract, and a test that needs it is asserting a mechanism. The test should
    be rewritten to drive the contract above; the entry records why it has not
    been yet.

The names are verified against the suite on both sides, so the *names* cannot
drift from reality and only the reasons are hand-maintained. That drift guard is
what stops this file becoming another hand-maintained mirror of generated truth.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

Kind = Literal["unreviewed", "harness lifecycle", "implementation step", "chokepoint state", "private type"]


class Declaration(NamedTuple):
    """One declared private import: the name, why, and who is carrying it."""

    kind: Kind
    reason: str
    retires: str | None


def _d(kind: Kind, reason: str, retires: str | None = None) -> Declaration:
    return Declaration(kind, reason, retires)


# Reviewed, and staying private on purpose. Everything else is still UNREVIEWED.
DECLARED: dict[str, Declaration] = {
    "app.crawl.extraction._EXTRACTION_EXECUTOR": _d(
        "harness lifecycle",
        "The suite starts the doc-extraction pool through this module, so only this "
        "module can stop it. conftest teardown is the one caller that legitimately "
        "reaches inside, because it owns the thing being torn down.",
    ),
    "app.crawl.extraction._shutdown_doc_extraction_executor": _d(
        "harness lifecycle",
        "The counterpart to the executor above, for the same reason: session "
        "teardown cannot ask a public seam to stop a pool it did not create.",
    ),
    "app.crawl.extraction._configure_doc_worker_logging": _d(
        "harness lifecycle",
        "Configures logging inside spawned doc workers. Asserted directly because the "
        "configuration is the contract and it is not reachable from a return value.",
    ),
    "app.crawl.extraction._FrameCollector": _d(
        "implementation step",
        "Accumulates frames across the scraping walk. A test reaching it is asserting "
        "the walk's internals; retires with #77 when frame scraping gains a seam.",
    ),
    "app.crawl.extraction._walkable": _d(
        "implementation step",
        "Documented as the precondition a diagnostic extractor needs before it can "
        "look at anything -- that is an input check, not a caller-facing operation. "
        "Retires with #77.",
    ),
}


# ---------------------------------------------------------------------------
# Seeded from the suite's state when the ledger was created. Every row here is
# recorded debt, not an argument: `unreviewed` means nobody has yet looked at the
# name and written the reason. A module's names leave this list when they become
# public (see issues #70-#82), and rows that stay get their kind and reason
# filled in, which is what turns a line item into a decision.
# ---------------------------------------------------------------------------
_CHOKEPOINT_STATE = _d(
    "chokepoint state",
    "Module-private data -- a cap, a cache, a lock, a counter. Not a missing seam. A constant is\n"
    "private because nobody outside the module may change it, and promoting one would be exactly the\n"
    "alias this ledger exists to prevent: the name would be public and still unwelded. A test reading\n"
    "it is asserting a configured fact about a chokepoint, which is white-box by nature and fine --\n"
    "what would not be fine is the suite being able to write it.\n"
    "\n"
    "The check that this is really state and not a hole: the name appears in a public default, a\n"
    "public return type, or nowhere at all. A constant that a caller is expected to *set* is not\n"
    "chokepoint state, it is a missing seam, and it belongs in the public surface instead.\n",
)

_IMPLEMENTATION_STEP = _d(
    "implementation step",
    "A step inside an algorithm rather than a contract. A test reaching one is asserting a\n"
    "mechanism, and the fix is to rewrite the test to drive the contract above and delete this row\n"
    "-- not to promote the step, which would make the mechanism part of the module's promise.\n",
)

_PRIVATE_TYPE = _d(
    "private type",
    "A private class, or a private method on a public one. A private *method* is an implementation\n"
    "step by definition: the class is public and the method is not, so the method is a step. A\n"
    "private *class* is a judgement -- it stays private if nothing constructs it from outside, and\n"
    "becomes public if it has a lifecycle the caller owns, as `_ResultSpill` does.\n",
)


UNREVIEWED: dict[str, Declaration] = {
    "app.api.async_crawl_runner._async_crawl_semaphore": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.api.async_crawl_runner._async_crawl_semaphore_loop": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.api.job_runner._tasks": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.content_cache.ContentCache._remember_memory_locked": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.decompression._DECODABLE_ENCODINGS": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._LAST_SPILL_GC_MONOTONIC": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._ResultSpill": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.engine._ResultSpill._gc_stale_spills": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.engine._RobotsPathMatcher": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._SHARED_HTTP_CACHE": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._SPILL_GC_MIN_INTERVAL_SECONDS": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._SPILL_PREFIX": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._resource_gate": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._robots_allows": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine._robots_pattern_to_regex": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine.robots_cache._entries": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.engine.robots_cache._lock": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.extraction._DOC_POOL": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.extraction._DocWorker": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.extraction._EXTRACTION_DOC_ACQUIRE_TIMEOUT": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.extraction._EXTRACTION_DOC_MAX_INPUT_BYTES": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.extraction._EXTRACTION_DOC_TIMEOUT": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.extraction._POOL_LOCK": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.fetch_helpers._SITEMAP_CACHE": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.job_store._Expected": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.policy._ERROR_CODE_BY_FIELD": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.result_helpers._CLASSIFICATION_BY_EXCEPTION": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.robots.robots_cache._entries": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.browser.BrowserRenderer._extract_text": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.strategies.browser.BrowserRenderer._get_browser_kwargs": _d(
        _PRIVATE_TYPE[0], _PRIVATE_TYPE[1]
    ),
    "app.crawl.strategies.browser.BrowserRenderer._harvest_timeout_for": _d(
        _PRIVATE_TYPE[0], _PRIVATE_TYPE[1]
    ),
    "app.crawl.strategies.browser.BrowserRenderer._make_response_handler": _d(
        _PRIVATE_TYPE[0], _PRIVATE_TYPE[1]
    ),
    "app.crawl.strategies.browser.BrowserRenderer._scroll_config_for": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.strategies.browser._BROWSER_COOKIES_BY_DOMAIN": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.browser._BROWSER_COOKIES_BY_DOMAIN._lock": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.browser._BROWSER_COOKIES_MAX": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.browser_actions.BrowserActionsMixin._dispatch_action": _d(
        _PRIVATE_TYPE[0], _PRIVATE_TYPE[1]
    ),
    "app.crawl.strategies.browser_actions._MAX_ACTION_WAIT_SECONDS": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.cookie_jar._COOKIE_JAR_MAX_HOSTS": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.cookie_jar._COOKIE_NAMESPACE": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.cookie_jar._GLOBAL_COOKIE_JAR": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.cookie_jar._GLOBAL_COOKIE_JAR._store": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.fetch_policy._RETRY_BACKOFF_CAP_SECONDS": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.http_client_pool._DEFAULT_CFFI_SESSION": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.http_client_pool._DEFAULT_HTTP_CLIENT": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.http_client_pool._LoopBoundPooledClient": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.strategies.http_client_pool._NoStoreCookieJar": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.crawl.strategies.http_client_pool._PENDING_CLIENT_CLOSE": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.http_client_pool._PENDING_CLIENT_CLOSE_LOCK": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.http_client_pool._PENDING_CLIENT_CLOSE_MAX": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.pinned_transport._CREDENTIAL_HEADERS": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.pinned_transport._DNS_CACHE": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.pinned_transport._DNS_CACHE._lock": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.crawl.strategies.pinned_transport._MAX_REDIRECT_HOPS": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.crawl.strategies.pinned_transport._build_pinned_request_async": _d(
        _CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]
    ),
    "app.logging_config._BC_OWNED_HANDLER": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.models._LITERAL_FIELDS": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.models._LiteralChecked": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.resource_pressure._BROWSER_RAM_FRACTION": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.resource_pressure._BROWSER_TASK_RAM_BYTES": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.resource_pressure._DOC_TASK_RAM_BYTES": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.resource_pressure._HTTP_TASK_RAM_BYTES": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.resource_pressure._MAX_DERIVED_POOL_SIZE": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.scheduler.DomainScheduler._purge_expired": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.scheduler._COOLDOWN_PURGE_INTERVAL": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.scheduler._MAX_COOLDOWN_WAIT": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.scheduler._STATUS_ACCOUNTING": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.scheduler._TokenBucket": _d(_PRIVATE_TYPE[0], _PRIVATE_TYPE[1]),
    "app.search.providers._MAX_TEXT_LENGTH": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.validation._PSL": _d(_CHOKEPOINT_STATE[0], _CHOKEPOINT_STATE[1]),
    "app.api.async_crawl_runner._get_async_crawl_semaphore": _d("unreviewed", "Not yet triaged."),
    "app.api.job_runner._job_engine_payload": _d("unreviewed", "Not yet triaged."),
    "app.api.job_runner._run_job": _d("unreviewed", "Not yet triaged."),
    "app.api.main_helpers._build_job_status": _d("unreviewed", "Not yet triaged."),
    "app.api.render._render_options": _d("unreviewed", "Not yet triaged."),
    "app.api.response_shaping._aunified_response_from_result": _d("unreviewed", "Not yet triaged."),
    "app.api.response_shaping._failed_unified_result": _d("unreviewed", "Not yet triaged."),
    "app.api.response_shaping._shape_all": _d("unreviewed", "Not yet triaged."),
    "app.api.webhooks._max_webhook_size": _d("unreviewed", "Not yet triaged."),
    "app.cgroup_sense._invalidate_cgroup_cache": _d("unreviewed", "Not yet triaged."),
    "app.cgroup_sense._read_int": _d("unreviewed", "Not yet triaged."),
    "app.classify._strip_html_noise": _d("unreviewed", "Not yet triaged."),
    "app.crawl.admission._fetch_and_decode": _d("unreviewed", "Not yet triaged."),
    "app.crawl.admission._frontier_identity": _d("unreviewed", "Not yet triaged."),
    "app.crawl.decompression._decompress_bounded": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._collect_document_item_lines": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._document_item_range": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._format_extracted_document_text": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._is_document_item_marker": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._is_document_non_item_section": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._join_document_paragraph_lines": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._looks_like_document_heading": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._next_non_empty_document_line": _d("unreviewed", "Not yet triaged."),
    "app.crawl.document_text._should_join_document_paragraph": _d("unreviewed", "Not yet triaged."),
    "app.crawl.engine._get_shared_content_cache": _d("unreviewed", "Not yet triaged."),
    "app.crawl.engine._parse_lastmod": _d("unreviewed", "Not yet triaged."),
    "app.crawl.engine._payload_policy": _d("unreviewed", "Not yet triaged."),
    "app.crawl.engine._purge_robots_cache": _d("unreviewed", "Not yet triaged."),
    "app.crawl.engine._seed_is_fresh": _d("unreviewed", "Not yet triaged."),
    "app.crawl.engine._stale_seeds": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._EXTRACTION_EXECUTOR": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._FrameCollector": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._acquire_doc_worker": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._configure_doc_worker_logging": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._doc_pool_health": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._doc_pool_pids": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._doc_task_hooks": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._parse_html_tree": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._reset_doc_pool": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._retire_doc_worker": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._shutdown_doc_extraction_executor": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._spawn_doc_worker": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._terminate_doc_process": _d("unreviewed", "Not yet triaged."),
    "app.crawl.extraction._walkable": _d("unreviewed", "Not yet triaged."),
    "app.crawl.fetch_helpers._fetch_and_decode": _d("unreviewed", "Not yet triaged."),
    "app.crawl.fetch_helpers._fetch_decode_discovery": _d("unreviewed", "Not yet triaged."),
    "app.crawl.fetch_helpers._fetch_text": _d("unreviewed", "Not yet triaged."),
    "app.crawl.llm_format._extract_llm_metadata": _d("unreviewed", "Not yet triaged."),
    "app.crawl.payload_helpers._payload_policy": _d("unreviewed", "Not yet triaged."),
    "app.crawl.payload_helpers._payload_urls": _d("unreviewed", "Not yet triaged."),
    "app.crawl.recursive_admission._batch_is_http_heavy": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._binary_no_text_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._classify_error_code": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._crawl_result_from_engine_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._crawl_result_from_exception": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._engine_result_from_exception": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._failed_engine_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._failed_result_for": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._make_engine_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._skipped_engine_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._terminal_crawl_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.result_helpers._terminal_engine_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.body_decode._content_metrics": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.body_decode._decode_body": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.body_decode._quality_score": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.browser._assert_safe_final_url": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.browser._is_ssrf_blocked": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.browser._make_ssrf_route_handler": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.browser._make_ssrf_websocket_handler": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.browser._resolve_browser_target": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.browser._store_browser_cookies": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.browser_pool._derive_browser_pool_size": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.content_cache_key._content_cache_key": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.content_cache_key._effective_max_content_length": _d(
        "unreviewed", "Not yet triaged."
    ),
    "app.crawl.strategies.document._default_document_text_extractor": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.document._extract_pdf_bytes_to_markdown": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.document._format_table": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.document._pdf_to_markdown": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.fetch_policy._discovery_diagnostics": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.fetch_policy._fetch_with_retries": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.fetch_policy._looks_like_binary_body": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.fetch_policy._retry_after_seconds": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.fetch_policy._retry_backoff_seconds": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.fetchers._cffi_fetch": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.fetchers._default_http_fetcher": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http._http_strategy_result": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http_client_pool._build_async_client": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http_client_pool._build_cffi_session": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http_client_pool._default_settings": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http_client_pool._drain_pending_client_closes": _d(
        "unreviewed", "Not yet triaged."
    ),
    "app.crawl.strategies.http_client_pool._queue_superseded_close": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http_client_pool._release_dropped_client": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http_client_pool._safe_aclose": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.http_client_pool._schedule_client_close": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.pinned_transport._build_pinned_request": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.pinned_transport._check_ssrf_protection": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.pinned_transport._declared_length_exceeds": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.pinned_transport._is_private_or_loopback_ip": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.pinned_transport._redirect_target": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.pinned_transport._resolve_and_pin_host": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.pinned_transport._resolve_ips": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.request_profile._apply_credentials": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.request_profile._cookie_header_for_url": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.request_profile._headers_for_strategy": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.request_profile._select_fingerprint": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategies.request_profile._store_set_cookies": _d("unreviewed", "Not yet triaged."),
    "app.crawl.strategy_chain._starting_strategies": _d("unreviewed", "Not yet triaged."),
    "app.main._warn_if_bound_off_loopback": _d("unreviewed", "Not yet triaged."),
    "app.mcp.tools._handle_crawl": _d("unreviewed", "Not yet triaged."),
    "app.mcp.tools._handle_map": _d("unreviewed", "Not yet triaged."),
    "app.mcp.tools._handle_render": _d("unreviewed", "Not yet triaged."),
    "app.mcp.tools._handle_search": _d("unreviewed", "Not yet triaged."),
    "app.models._literal_members": _d("unreviewed", "Not yet triaged."),
    "app.output_formats._csv_neutralize": _d("unreviewed", "Not yet triaged."),
    "app.output_formats._default": _d("unreviewed", "Not yet triaged."),
    "app.output_formats._safe": _d("unreviewed", "Not yet triaged."),
    "app.resource_pressure._build_admission_policy": _d("unreviewed", "Not yet triaged."),
    "app.search.providers._collect_results": _d("unreviewed", "Not yet triaged."),
    "app.search.providers._provider_http": _d("unreviewed", "Not yet triaged."),
    "app.settings._default_http_keepalive": _d("unreviewed", "Not yet triaged."),
    "app.settings._default_http_max_connections": _d("unreviewed", "Not yet triaged."),
    "app.validation._host_is_public": _d("unreviewed", "Not yet triaged."),
    "app.validation._is_public_ip_value": _d("unreviewed", "Not yet triaged."),
    "app.validation._reset_guard_memo": _d("unreviewed", "Not yet triaged."),
}


LEDGER: dict[str, Declaration] = {**UNREVIEWED, **DECLARED}
