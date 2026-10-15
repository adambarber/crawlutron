"""The first half of the private-import ledger, A-M per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _CHOKEPOINT_STATE,
    _PRIVATE_TYPE,
    _SUBSTITUTED_CAP,
    _SUBSTITUTED_COLLABORATOR,
    Declaration,
    _d,
)

DECLARED: dict[str, Declaration] = {
    "app.api.async_crawl_runner._async_crawl_semaphore": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.api.async_crawl_runner._async_crawl_semaphore_loop": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.api.async_crawl_runner._build_async_crawl_semaphore": _SUBSTITUTED_COLLABORATOR,
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
    "app.api.job_lifecycle._tasks": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
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
    "app.api.response_shaping._failed_unified_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.api.webhooks._max_webhook_size": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.extraction.classify._strip_html_noise": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.admission.admission_links._frontier_identity": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.content_cache.ContentCache._remember_memory_locked": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.orchestration.engine._LAST_SPILL_GC_MONOTONIC": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.engine._ResultSpill": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.orchestration.engine._ResultSpill._gc_stale_spills": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.orchestration.engine._SPILL_GC_MIN_INTERVAL_SECONDS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.engine._SPILL_PREFIX": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.orchestration.engine._get_shared_content_cache": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.engine._parse_lastmod": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.engine._purge_robots_cache": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.engine._resource_gate": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.orchestration.engine._seed_is_fresh": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.engine._stale_seeds": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.engine.robots_cache._entries": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.transport.browser_cookies.BROWSER_COOKIES_BY_DOMAIN._lock": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.engine.robots_cache._lock": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.fetch_helpers._fetch_decode_discovery": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.fetch_helpers._fetch_text": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.storage.job_store._Expected": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.admission.payload_helpers._payload_urls": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.admission.policy._ERROR_CODE_BY_FIELD": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.admission.recursive_admission._batch_is_http_heavy": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.orchestration.classification._CLASSIFICATION_BY_EXCEPTION": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.engine_result._failed_engine_result": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.engine_result._skipped_engine_result": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.engine_result._terminal_engine_result": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.orchestration.robots._RobotsPathMatcher": _d(
        "private type",
        "A private class nothing outside the module constructs -- production only ever builds a\n"
        "matcher through _RobotsPatternCache. The suite has to construct one because the parity\n"
        "oracle generates group compositions and feeds them in; that is the test's subject, not a\n"
        "hole in the class's surface.\n"
        "\n"
        "This used to be reached through app.orchestration.engine, which re-exported six robots privates\n"
        "for no production reason at all -- the aliases were dead in engine and alive only in the\n"
        "suite. Reaching the class where it is defined removes an alias chain rather than adding\n"
        "a door.\n",
        ["test"],
    ),
    "app.orchestration.robots._robots_pattern_to_regex": _d(
        "implementation step",
        "Pattern-to-matcher compilation, reached to prove per-host binding and that a pathological\n"
        "pattern stays linear. Asserting on the compiled matcher is asserting the mechanism; the\n"
        "step stays private and the suite drives it here because the property under test -- one\n"
        "cache slot per (host, pattern) -- has no expression through robots_allows.\n",
        ["test"],
    ),
    "app.orchestration.robots.robots_cache._entries": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.storage.sqlite_store._BUSY_TIMEOUT_MS": _SUBSTITUTED_CAP,
    "app.storage.sqlite_store._WAL_RETRY_SECONDS": _SUBSTITUTED_CAP,
    "app.transport.browser_driver.BrowserDriver._dispatch_action": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.transport.browser_pool._derive_browser_pool_size": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.transport.content_cache_key._RAM_SAFETY_BYTES": _SUBSTITUTED_CAP,
    "app.transport.content_cache_key._effective_max_content_length": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test", "app"],
    ),
}
