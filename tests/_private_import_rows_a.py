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
    "app.transport.browser_cookies.BROWSER_COOKIES_BY_DOMAIN._lock": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
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
}
