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
    "app.transport.browser_cookies.BROWSER_COOKIES_BY_DOMAIN._lock": _d(
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
}
