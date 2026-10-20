"""The first half of the private-import ledger, A-M per module key."""

from __future__ import annotations

from tests._private_import_decl import (
    _CHOKEPOINT_STATE,
    _INSTANCE_STATE,
    _PRIVATE_TYPE,
    _SUBSTITUTED_CAP,
    _SUBSTITUTED_COLLABORATOR,
    Declaration,
    _d,
)

DECLARED: dict[str, Declaration] = {
    "app.admission.admission_links._ROBOTS_CHUNK_DEADLINE_SLACK_SECONDS": _d(
        "implementation constant",
        "The per-chunk robots-admission wall-clock slack; a test checks it off to shrink the "
        "deadline from timeouts+5s to milliseconds so the bounded-latency property is asserted "
        "quickly rather than with a real 5-second wait.",
        ["test"],
    ),
    "app.api.async_crawl_runner._build_async_crawl_semaphore": _SUBSTITUTED_COLLABORATOR,
    "app.api.job_lifecycle._tasks": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.transport.content_cache.ContentCache._remember_memory_locked": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.transport.decompression._decompress_brotli": _d(
        "implementation step",
        "Proof that the streaming brotli branch bounds each process() call's peak to O(cap) by feeding"
        " the size argument (only exposable by instrumenting the decoder) and then raises"
        " DecompressionTooLargeError for bombs. The remote-OOM property has no expression through the"
        " public surface; the stream is driven here with the decoder substituted at the dependency"
        " boundary.\n",
        ["test"],
    ),
    "app.transport.browser_cookies.BROWSER_COOKIES_BY_DOMAIN._store": _d(
        "private state",
        "#123: probe inspects the medium the process-global browser store shares with the jar.",
        ["test"],
    ),
    "app.transport.browser_cookies.BROWSER_COOKIES_BY_DOMAIN._store._lock": _d(
        "private state",
        "#123: the concurrency probe swaps the medium's lock for a recording double.",
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
    "app.transport.content_cache.ContentCache._index": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._lock": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._index_sizes": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._index_cached_at": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._memory_cache": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._path_for": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._drop_locked": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._unlink_doomed": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._read_envelope": _INSTANCE_STATE,
    "app.transport.content_cache.ContentCache._MEMORY_CACHE_MAX_SIZE": _INSTANCE_STATE,
    "app.transport.cookie_jar.CookieJar._inmemory": _INSTANCE_STATE,
    "app.transport.browser_pool.BrowserPool._available": _INSTANCE_STATE,
    "app.transport.browser_pool.BrowserPool._pool_size": _INSTANCE_STATE,
    "app.transport.browser_pool.BrowserPool._renderers": _INSTANCE_STATE,
    "app.transport.cache.HttpCache._data": _INSTANCE_STATE,
    "app.orchestration.scheduler.DomainScheduler._domain_cooldowns": _INSTANCE_STATE,
    "app.orchestration.scheduler.DomainScheduler._circuit_threshold": _INSTANCE_STATE,
    "app.runtime.memory_pressure.MemoryGate._lag_probe": _INSTANCE_STATE,
    "app.runtime.resource_pressure.LoopLagProbe._lag": _INSTANCE_STATE,
    "app.runtime.resource_pressure.LoopLagProbe._task": _INSTANCE_STATE,
    "app.runtime.resource_pressure.ResourceGate._lag_probe": _INSTANCE_STATE,
    "app.runtime.resource_pressure.WeightedAdmission._p": _INSTANCE_STATE,
    "app.storage.job_store.JobStore._conn": _INSTANCE_STATE,
    "app.runtime.memory_pressure._total_system_ram": _d(
        "chokepoint state",
        "A base RAM figure the admission test shrinks via string-setattr so the cap fires quickly.",
        ["test"],
    ),
    "app.api.job_runner._execute_crawl": _d(
        "substituted seam",
        "The engine entry the runner calls at job time; the requeue test substitutes it for a fresh run.",
        ["test"],
    ),
    "app.storage.job_store.JobStore._row_to_job": _d(
        "implementation step",
        "A row-to-model mapping the isolation test drives to round out an ownership assertion with no seam.",
        ["test"],
    ),
    "app.transport.browser.BrowserRenderer._looks_like_pdf": _d(
        "implementation step",
        "A classifier predicate the test drives directly because the is-pdf verdict has no message seam.",
        ["test"],
    ),
    "app.transport.browser.BrowserRenderer._render_with_browser": _d(
        "substituted seam",
        "Test doubles replace the renderer's browser-collaborator driver rather than reading returns.",
        ["test"],
    ),
}
