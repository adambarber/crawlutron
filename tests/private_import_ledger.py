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
    A step inside an algorithm rather than a contract. A test reaching one is asserting a
    mechanism; the fix is to rewrite the test to drive the contract above, not to promote
    the step.

``substituted seam``
    The suite replaces the name rather than reading it -- a collaborator swapped for a
    double, or a cap shrunk until the path behind it is reachable inside a test. This is
    the largest single category of reach in the suite, and the one the gate could not see
    at all until ``monkeypatch.setattr(module, "_name", ...)`` was recognised as a
    spelling rather than treated as a string. It stays private on purpose: a public
    collaborator is a collaborator production can be broken by, so the reach is correct
    and the fix for it is nothing. Promoting the name would quiet the gate and lose the
    property.
``unreviewed``
    Recorded from the tree when this file was created. No one has argued for it
    yet, and these are the debt.

The names are verified against both trees in both directions on every run, so the
*names* cannot drift from reality and only the reasons are hand-maintained. That
guard is what stops this becoming another hand-kept mirror of generated truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, NamedTuple

Kind = Literal[
    "unreviewed",
    "harness lifecycle",
    "chokepoint state",
    "private type",
    "implementation step",
    "substituted seam",
]
Side = Literal["test", "app"]


class Declaration(NamedTuple):
    """One private reach: why it may stay, and which hand reaches for it."""

    kind: Kind
    reason: str
    side: tuple[Side, ...] = ()


def _d(kind: Kind, reason: str, side: Sequence[Side] = ()) -> Declaration:
    """One declaration, with ``side`` normalised to the immutable field type.

    The parameter is a ``Sequence`` and the field a ``tuple`` because the table's
    rows want to read as literals -- ``["test"]`` -- while ``Declaration.side``
    wants to stay hashable and unmutable once built. Normalising here is what lets
    every row be written the readable way without the annotation being a claim
    about 158 call sites that none of them satisfies.
    """
    return Declaration(kind, reason, tuple(side))


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


_SUBSTITUTED_COLLABORATOR = _d(
    "substituted seam",
    "The suite replaces this name with a double rather than reading what it returns.\n"
    "A read asserts on a mechanism; a substitution asserts that the code under test\n"
    "takes this collaborator at all, which is a contract -- and the only way to observe\n"
    "that contract from outside the module is to *be* the collaborator.\n"
    "\n"
    "It stays private on purpose. A public collaborator is one production can reach too,\n"
    "and a double production can reach is a double production can be broken by. So the\n"
    "reach is correct and the fix for it is nothing; promoting the name would quiet this\n"
    "gate and lose the property that made the gate worth having.\n",
    side=("test",),
)


_SUBSTITUTED_CAP = _d(
    "substituted seam",
    "A private cap, timeout or retry delay the suite shrinks until the path behind it is\n"
    "reachable inside a test. The shipped value is minutes, or tens of megabytes; a test\n"
    "that waits that long does not run, so the suite substitutes a value small enough for\n"
    "the guard to fire or the retry to happen.\n"
    "\n"
    "This is deliberately not `chokepoint state`, which is about *reading* module-private\n"
    "data. Here the suite writes a name no caller may set: raising it is not a missing\n"
    "seam, it is a caller moving a bound the module owns. The name stays private and the\n"
    "substitution is recorded.\n",
    side=("test",),
)


_OWNED_POOL_MACHINERY = _d(
    "harness lifecycle",
    "A pool's own machinery, driven by the test that exists to prove it.\n"
    "\n"
    "Two pools are reached this way and the argument is identical for both: the\n"
    "doc-extraction pool (``tests/crawl/test_doc_pool.py``) and the HTTP client pool\n"
    "(``tests/crawl/test_http_client_pool.py``). Each test exists to prove an acquire\n"
    "lock, a spawn-once rule and a close path, and none of that is reachable from\n"
    "above -- ``extract_content`` hands back a document rather than a worker handle, a\n"
    "request hands back a response rather than a pooled client. So there is no seam\n"
    "these tests could work through instead.\n"
    "\n"
    "The reach is honest and the verdict is that nothing needs promoting. What these\n"
    "rows must not become is a public pool API: a caller that can acquire and retire\n"
    "workers, or evict a client out from under an in-flight close, is a caller that\n"
    "can wedge the pool -- which is the failure the locks and the escalation exist to\n"
    "contain. The names stay private and each pool keeps its own test.\n"
    "\n"
    "Held as `harness lifecycle` in the sense the kind already means -- the suite\n"
    "drives a world it started and cannot reach any other way. Filed as a possible\n"
    'sixth kind ("the owner\'s own test") in #86, deferred rather than grown for the\n'
    "three rows that would justify it today.\n",
    side=("test",),
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
    "app.api.middleware._MAX_REQUEST_BODY_BYTES": _SUBSTITUTED_CAP,
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
    "app.crawl.engine._LAST_SPILL_GC_MONOTONIC": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.engine._ResultSpill": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.crawl.engine._ResultSpill._gc_stale_spills": _d(_PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]),
    "app.crawl.engine._SPILL_GC_MIN_INTERVAL_SECONDS": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.crawl.engine._SPILL_PREFIX": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.engine._fetch_and_decode": _SUBSTITUTED_COLLABORATOR,
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
    "app.crawl.extraction._FrameCollector": _d(
        _IMPLEMENTATION_STEP.kind, _IMPLEMENTATION_STEP.reason, ["test"]
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
    "app.crawl.result_helpers._failed_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._skipped_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.result_helpers._terminal_engine_result": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.robots._RobotsPathMatcher": _d(
        "private type",
        "A private class nothing outside the module constructs -- production only ever builds a\n"
        "matcher through _RobotsPatternCache. The suite has to construct one because the parity\n"
        "oracle generates group compositions and feeds them in; that is the test's subject, not a\n"
        "hole in the class's surface.\n"
        "\n"
        "This used to be reached through app.crawl.engine, which re-exported six robots privates\n"
        "for no production reason at all -- the aliases were dead in engine and alive only in the\n"
        "suite. Reaching the class where it is defined removes an alias chain rather than adding\n"
        "a door.\n",
        ["test"],
    ),
    "app.crawl.robots._fetch_and_decode": _SUBSTITUTED_COLLABORATOR,
    "app.crawl.robots._robots_pattern_to_regex": _d(
        "implementation step",
        "Pattern-to-matcher compilation, reached to prove per-host binding and that a pathological\n"
        "pattern stays linear. Asserting on the compiled matcher is asserting the mechanism; the\n"
        "step stays private and the suite drives it here because the property under test -- one\n"
        "cache slot per (host, pattern) -- has no expression through robots_allows.\n",
        ["test"],
    ),
    "app.crawl.robots.robots_cache._entries": _d(_CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]),
    "app.crawl.sqlite_store._BUSY_TIMEOUT_MS": _SUBSTITUTED_CAP,
    "app.crawl.sqlite_store._WAL_RETRY_SECONDS": _SUBSTITUTED_CAP,
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
    "app.crawl.strategies.browser._store_browser_cookies": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.browser_driver.BrowserDriver._dispatch_action": _d(
        _PRIVATE_TYPE.kind, _PRIVATE_TYPE.reason, ["test"]
    ),
    "app.crawl.strategies.browser_pool._derive_browser_pool_size": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
    "app.crawl.strategies.content_cache_key._RAM_SAFETY_BYTES": _SUBSTITUTED_CAP,
    "app.crawl.strategies.content_cache_key._effective_max_content_length": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test", "app"],
    ),
    "app.crawl.strategies.cookie_jar._COOKIE_JAR_MAX_HOSTS": _d(
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
    "app.crawl.strategies.fetchers._release_stream_noblock": _SUBSTITUTED_COLLABORATOR,
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
    "app.crawl.strategies.http_client_pool._build_async_client": _SUBSTITUTED_COLLABORATOR,
    "app.crawl.strategies.http_client_pool._build_cffi_session": _SUBSTITUTED_COLLABORATOR,
    "app.crawl.strategies.http_client_pool._drain_pending_client_closes": _d(
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.kind,
        _LIFECYCLE_DRAIN_PENDING_CLIENT_CLOSES.reason,
        ["test", "app"],
    ),
    "app.crawl.strategies.http_client_pool._queue_superseded_close": _SUBSTITUTED_COLLABORATOR,
    "app.crawl.strategies.http_client_pool._release_dropped_client": _OWNED_POOL_MACHINERY,
    "app.crawl.strategies.http_client_pool._schedule_client_close": _OWNED_POOL_MACHINERY,
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
    "app.mcp.server._call_tool": _SUBSTITUTED_COLLABORATOR,
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
    "app.resource_pressure._BROWSER_TASK_RAM_BYTES": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._DOC_TASK_RAM_BYTES": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._HTTP_TASK_RAM_BYTES": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
    ),
    "app.resource_pressure._LOOP_LAG_PROBE_ENABLED": _SUBSTITUTED_CAP,
    "app.resource_pressure._MAX_DERIVED_POOL_SIZE": _d(
        _CHOKEPOINT_STATE.kind, _CHOKEPOINT_STATE.reason, ["test"]
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
    "app.validation._reset_guard_memo": _d(
        "unreviewed",
        "Not yet triaged.\n",
        ["test"],
    ),
}
