"""Executable safety invariants: recurring bug classes blocked at the door.

The round-4 review's central lesson (validated empirically): static
rejection of "pathological" shapes is bypassable, and chokepoints only
hold if bypassing them fails CI instead of code review. These AST walks
verify the UNSAFE patterns are absent from app/ so reintroducing them
turns the build red.
"""

from __future__ import annotations

import ast
import pathlib
import re
from contextvars import ContextVar

import pytest
from chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    STRATEGIES_DIR,
    TESTS_DIR,
    app_offenders,
    app_python_files,
    app_trees,
    call_keyword_values,
    calls_named,
    file_tree,
    offender,
    resolved_call_name,
    trees_under,
)
from property_registry import PROPERTY_BY_NAME, PROPERTY_REGISTRY, Property

from app.settings import ENVIRONMENT_FIELD_ALIASES

__all__ = [
    "REPO_ROOT",
    "SANCTIONED",
    "STRATEGIES_DIR",
    "app_python_files",
    "app_trees",
    "app_offenders",
    "call_keyword_values",
    "calls_named",
    "file_tree",
    "offender",
    "trees_under",
]

# Per-file no-growth budget for app/. A new file starts at 0; a file may not grow.
# The budget is a ratchet that moves ONLY in the commit that legitimately shrinks
# or justifies a file, so a gate is never a reason to stop removing code. Seven
# entries moved in the policy-enforcement commit, and they did NOT all move for
# the same reason -- a review checked each one, and an earlier blanket
# justification here was wrong. The actual reasons, per file:
#
#   payload_helpers.py  the shared `_validated_url` admission helper itself
#   jobs_router.py      admission wiring: refuse before the job is created
#   migrations/runner.py    startup conformance, partial stamp, truthful CLI status
#   migrations/manifest.py  column comparison consolidated to one by-name function
#   extraction.py       frame URL disclosure filtering (a DIFFERENT fix)
#   fetch_policy.py     feed URL disclosure filtering (a DIFFERENT fix)
#   render.py           shared admission validation rejects a refused target
#                       before render work is scheduled, so no refusal becomes a
#                       per-URL result or a successful submission
#   mcp/tools.py        one tool-error envelope for every MCP refusal
#   main_helpers.py     shared crawl completion metrics and logging
#   exceptions.py       shared validation error-envelope construction
_APP_FILE_LINE_BUDGETS = {
    "app/__init__.py": 1,
    "app/api/async_crawl_runner.py": 199,
    "app/api/crawl_endpoint.py": 195,
    "app/api/crawl_executor.py": 85,
    "app/api/crawl_router.py": 134,
    "app/api/dependencies.py": 37,
    "app/api/exceptions.py": 128,
    "app/api/job_runner.py": 469,
    "app/api/jobs_router.py": 173,
    "app/api/main_helpers.py": 46,
    "app/api/map_endpoint.py": 75,
    "app/api/map_router.py": 35,
    "app/api/metrics_router.py": 21,
    "app/api/middleware.py": 83,
    "app/api/policy_defaults.py": 34,
    "app/api/render.py": 199,
    "app/api/render_endpoint.py": 85,
    "app/api/render_router.py": 34,
    "app/api/response_shaping.py": 393,
    "app/api/webhooks.py": 224,
    "app/api_models.py": 789,
    "app/atomic_write.py": 40,
    "app/bounded_io.py": 45,
    "app/bounded_ttl.py": 116,
    "app/cgroup_sense.py": 59,
    "app/classify.py": 335,
    "app/constants.py": 547,
    "app/crawl/__init__.py": 1,
    "app/crawl/admission.py": 543,
    "app/crawl/cache.py": 118,
    "app/crawl/content_cache.py": 518,
    "app/crawl/credentials.py": 88,
    "app/crawl/decompression.py": 162,
    "app/crawl/document_text.py": 180,
    "app/crawl/engine.py": 1166,
    "app/crawl/extraction.py": 1139,
    "app/crawl/fetch_helpers.py": 264,
    "app/crawl/ids.py": 23,
    "app/crawl/job_store.py": 697,
    "app/crawl/llm_format.py": 131,
    "app/crawl/migrations/0001_crawl_jobs_initial.py": 14,
    "app/crawl/migrations/0001_results_initial.py": 14,
    "app/crawl/migrations/0002_crawl_jobs_drop_canonicalized.py": 24,
    "app/crawl/migrations/__init__.py": 35,
    "app/crawl/migrations/__main__.py": 6,
    "app/crawl/migrations/manifest.py": 216,
    "app/crawl/migrations/runner.py": 183,
    "app/crawl/payload_helpers.py": 204,
    "app/crawl/recursive.py": 438,
    "app/crawl/render_options.py": 93,
    "app/crawl/result_helpers.py": 534,
    "app/crawl/result_store.py": 85,
    "app/crawl/robots.py": 238,
    "app/crawl/robots_parse.py": 159,
    "app/crawl/scheduler_exceptions.py": 21,
    "app/crawl/scope_match.py": 31,
    "app/crawl/sqlite_store.py": 112,
    "app/crawl/strategies/__init__.py": 1,
    "app/crawl/strategies/body_decode.py": 114,
    "app/crawl/strategies/browser.py": 1220,
    "app/crawl/strategies/browser_pool.py": 247,
    "app/crawl/strategies/content_cache_key.py": 85,
    "app/crawl/strategies/cookie_jar.py": 168,
    "app/crawl/strategies/document.py": 145,
    "app/crawl/strategies/fetch_policy.py": 245,
    "app/crawl/strategies/fetchers.py": 328,
    "app/crawl/strategies/http.py": 659,
    "app/crawl/strategies/http_client_pool.py": 482,
    "app/crawl/strategies/pinned_transport.py": 256,
    "app/crawl/strategies/request_profile.py": 206,
    "app/discovery/__init__.py": 1,
    "app/discovery/agent_hints.py": 106,
    "app/discovery/capabilities.py": 131,
    "app/discovery/well_known.py": 57,
    "app/health/__init__.py": 1,
    "app/health/readiness.py": 150,
    "app/json_safe.py": 35,
    "app/logging_config.py": 162,
    "app/main.py": 251,
    "app/map.py": 94,
    "app/mcp/__init__.py": 11,
    "app/mcp/constants.py": 59,
    "app/mcp/protocol.py": 140,
    "app/mcp/router.py": 72,
    "app/mcp/server.py": 259,
    "app/mcp/tools.py": 279,
    "app/memory_pressure.py": 234,
    "app/metrics.py": 169,
    "app/models.py": 433,
    "app/output_formats.py": 140,
    "app/resource_pressure.py": 633,
    "app/safe_regex.py": 74,
    "app/safe_url.py": 102,
    "app/scheduler.py": 435,
    "app/search/__init__.py": 19,
    "app/search/endpoint.py": 65,
    "app/search/providers.py": 420,
    "app/search/router.py": 46,
    "app/settings.py": 250,
    "app/timeouts.py": 47,
    "app/validation.py": 685,
}


def test_app_files_do_not_grow():
    offenders: list[str] = []
    for path, _tree in app_trees(exclude=frozenset()):
        relative = path.relative_to(REPO_ROOT).as_posix()
        actual = len(path.read_text(encoding="utf-8").splitlines())
        allowed = _APP_FILE_LINE_BUDGETS.get(relative, 0)
        if actual > allowed:
            offenders.append(f"{relative}: {actual} > {allowed}")
    assert not offenders, "app file line budget exceeded: " + ", ".join(offenders)


# Dynamic-pattern re.* calls: a non-literal first arg is a pattern built from
# attacker-influenced text (the ReDoS shape). `re.search(LITERAL, text)` is
# safe -- only a dynamic PATTERN is the hazard.
_RE_DYNAMIC_METHODS = {"compile", "search", "match", "fullmatch", "sub", "findall", "split"}


def _dynamic_pattern_re_calls(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag re.<method>(<non-literal pattern>, ...) -- the ReDoS shape."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.func.attr in _RE_DYNAMIC_METHODS
            and node.args
            and not isinstance(node.args[0], ast.Constant)
        ):
            offenders.append(offender(path, node.lineno, f"re.{node.func.attr}(<dynamic pattern>)"))
    return offenders


def test_no_dynamic_pattern_re_calls():
    """re.* with a non-literal pattern must go through app.safe_regex."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_dynamic_pattern_re_calls(tree, path))
    assert not offenders, "Use app.safe_regex.compile_untrusted/match_bounded: " + ", ".join(offenders)


def test_no_direct_regex_engine_import():
    """Only safe_regex.py may import the third-party `regex` engine."""
    offenders: list[str] = []
    for path, tree in app_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "regex":
                        offenders.append(offender(path, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.module == "regex":
                    offenders.append(offender(path, node.lineno))
    assert not offenders, "Only app.safe_regex may import the regex engine: " + ", ".join(offenders)


def test_no_bare_re_compile_on_nonliteral():
    """Non-literal re.compile must go through app.safe_regex.compile_untrusted."""
    offenders: list[str] = []
    for path, tree in app_trees():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "compile"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "re"
                and node.args
                and not isinstance(node.args[0], ast.Constant)
            ):
                offenders.append(offender(path, node.lineno))
    assert not offenders, "Use app.safe_regex.compile_untrusted for non-literal patterns: " + ", ".join(
        offenders
    )


def _lock_bodies_with_awaits(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag `with <lock>:` bodies containing an await (the M3 class).

    A THREADING lock held across an await blocks the event loop and can
    deadlock. `async with` (asyncio.Lock/Condition) bodies may legitimately
    await -- that is the sanctioned pattern -- so only sync `with` is
    checked. Sync-with bodies that await include any `threading.Lock`/
    `RLock` context (which is what ContentCache.aset did before its N1
    rework).

    A lock is recognized by case-insensitive name stem (lock/mutex/guard)
    *or* by binding to a threading lock-family constructor anywhere in the
    module, so `self._mutex = threading.RLock()` cannot rename its way out
    of the sweep.
    """
    offenders: list[str] = []
    lock_names = _threading_lock_bindings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        if isinstance(node, ast.AsyncWith):
            continue  # asyncio.Lock bodies may await by design
        for item in node.items:
            # `with self._lock:` / `with _CLOSE_LOCK:` / `with guard:` --
            # a sync lock context. Any await inside is a loop-blocker.
            expr = item.context_expr
            referred = getattr(expr, "attr", None) or getattr(expr, "id", None)
            if referred is None:
                continue
            looks_like_lock = any(stem in referred.lower() for stem in _LOCK_NAME_STEMS) or (
                referred in lock_names
            )
            if not looks_like_lock:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Await):
                    offenders.append(offender(path, inner.lineno))
            break
    return offenders


_LOCK_NAME_STEMS = ("lock", "mutex", "guard")
_THREADING_LOCK_CONSTRUCTORS = frozenset({"Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore"})


def _threading_lock_bindings(tree: ast.AST) -> set[str]:
    """Names/attributes bound to threading lock-family constructors."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        ctor = getattr(func, "attr", None) or getattr(func, "id", None)
        if ctor not in _THREADING_LOCK_CONSTRUCTORS:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_no_await_inside_lock_bodies():
    """A lock held across an await blocks the loop and can deadlock (M3)."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_lock_bodies_with_awaits(tree, path))
    assert not offenders, "Lock held across await: " + ", ".join(offenders)


def test_lock_detector_bites():
    """Name stems beyond 'lock' and threading-bound aliases must fire (M3)."""
    mutex = ast.parse(
        "class C:\n    def __init__(self):\n        self._mutex = __import__('threading').Lock()\n"
        "    async def go(self):\n        with self._mutex:\n            await x()\n"
    )
    assert _lock_bodies_with_awaits(mutex, REPO_ROOT / "app" / "_synthetic.py"), "_mutex escaped the sweep"
    bound = ast.parse(
        "import threading\n\n_async_gate = threading.RLock()\n\n"
        "async def go():\n    with _async_gate:\n        await x()\n"
    )
    assert _lock_bodies_with_awaits(bound, REPO_ROOT / "app" / "_synthetic.py"), (
        "threading-bound name escaped"
    )
    asyncio_ok = ast.parse(
        "import asyncio\n\nclass C:\n    def __init__(self):\n        self._lock = asyncio.Lock()\n"
        "    async def go(self):\n        async with self._lock:\n            await x()\n"
    )
    assert not _lock_bodies_with_awaits(asyncio_ok, REPO_ROOT / "app" / "_synthetic.py"), (
        "asyncio lock flagged"
    )
    plain_ok = ast.parse("def go():\n    with open('f'):\n        pass\n")
    assert not _lock_bodies_with_awaits(plain_ok, REPO_ROOT / "app" / "_synthetic.py")


# --- SafeUrl chokepoint (final review) ------------------------------------

# A hand-rolled redirect hop is the "forgot one of the five steps" class SafeUrl
# exists to kill. The dangerous *shape* -- in a SINGLE function -- is: test a
# 3xx status, build the next URL with a raw urljoin, and never go through the
# chokepoint (redirect_hop) or the sanctioned pinned helper (_redirect_target).
# Link/robots/sitemap urljoin normalization never co-occurs with a 3xx test in
# the same function, so it is not a false positive -- and this runs over the
# whole tree, not a hand-picked file list.
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_REDIRECT_HOP_SANCTIONED = {"redirect_hop", "_redirect_target"}


def _functions_with(tree: ast.AST) -> list[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module))]


def _uses_redirect_status(node: ast.AST) -> bool:
    """True if the function tests a concrete redirect status constant."""
    return any(isinstance(sub, ast.Constant) and sub.value in _REDIRECT_STATUSES for sub in ast.walk(node))


def _hand_rolled_redirect_hops(tree: ast.AST, path: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for func in _functions_with(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has_urljoin = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "urljoin"
            for n in ast.walk(func)
        )
        if not has_urljoin or not _uses_redirect_status(func):
            continue
        uses_chokepoint = any(
            isinstance(n, ast.Attribute)
            and n.attr in _REDIRECT_HOP_SANCTIONED
            or isinstance(n, ast.Name)
            and n.id in _REDIRECT_HOP_SANCTIONED
            for n in ast.walk(func)
        )
        if not uses_chokepoint:
            offenders.append(offender(path, func.lineno, f"{func.name}()"))
    return offenders


def test_no_hand_rolled_redirect_hops():
    """Redirect hops anywhere go through SafeUrl.redirect_hop (or the pinned
    fetch helper), never a raw urljoin combined with a 3xx status test."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED | {"safe_url.py"}):
        offenders.extend(_hand_rolled_redirect_hops(tree, path))
    assert not offenders, "Use SafeUrl.redirect_hop for redirect hops: " + ", ".join(offenders)


_CANONICALIZER = frozenset({"normalize_public_http_url"})


def _raw_batch_dedup_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``dict.fromkeys`` batch dedup that keys on raw values.

    The url-batch dedup at ``_payload_urls`` must collapse duplicates by
    canonical page identity (``normalize_public_http_url``) -- not the raw
    string -- or a request carrying both ``.../x`` and ``.../x/?utm_source=tw``
    crawls one page twice. Every ordered-dedup via ``dict.fromkeys`` must sit
    inside a function that also routes through the canonicalizer, so a new
    raw-key dedup fails the suite the moment it lands.
    """
    offenders: list[str] = []
    for func in _functions_with(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        uses_fromkeys = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "fromkeys"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "dict"
            for n in ast.walk(func)
        )
        if not uses_fromkeys:
            continue
        canonicalizes = any(
            isinstance(n, ast.Name)
            and n.id in _CANONICALIZER
            or isinstance(n, ast.Attribute)
            and n.attr in _CANONICALIZER
            for n in ast.walk(func)
        )
        if not canonicalizes:
            offenders.append(
                offender(
                    path,
                    func.lineno,
                    f"{func.name}() dedups a batch with dict.fromkeys, not the canonicalizer",
                )
            )
    return offenders


def test_batch_dedup_keys_on_canonical_page_identity():
    """URL-batch dedup must collapse by ``normalize_public_http_url``, never raw strings."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_raw_batch_dedup_offenders(tree, path))
    assert not offenders, (
        "dedup url batches via normalize_public_http_url, not raw dict.fromkeys: " + ", ".join(offenders)
    )


def test_batch_dedup_detector_bites():
    raw = ast.parse("def _payload_urls(payload):\n    return list(dict.fromkeys(payload['urls']))\n")
    assert _raw_batch_dedup_offenders(raw, REPO_ROOT / "app" / "crawl" / "_synthetic.py"), (
        "detector missed raw dict.fromkeys dedup"
    )
    good = ast.parse(
        "def _payload_urls(payload):\n"
        "    return [u for u in payload['urls'] if normalize_public_http_url(u) not in seen]\n"
    )
    assert not _raw_batch_dedup_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


def _bare_body_reads(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag an unbounded body acquisition on a response-like object.

    Both ``response.read()`` (no size arg) and ``response.content`` pull the
    whole body into RAM. Capped reads go through app/bounded_io.read_capped_*;
    curl_cffi's ``.body`` attribute and non-response reads (files, sockets)
    are not HTTP body acquisitions and stay exempt. Tree-wide: any module may

    grow a fetch path, so this is keyed on the response-object convention
    rather than a directory.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        recv = (
            node.func.value if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None
        )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "read":
            if not node.args and not node.keywords and _is_response_like(recv):
                offenders.append(offender(path, node.lineno, ".read()"))
        elif isinstance(node, ast.Attribute) and node.attr == "content" and _is_response_like(node.value):
            offenders.append(offender(path, node.lineno, ".content"))
    return offenders


def _is_response_like(node: ast.expr | None) -> bool:
    """True for a plain-name receiver that reads as an HTTP response."""
    if not isinstance(node, ast.Name):
        return False
    head = node.id.lower()
    return head.startswith("response") or head.startswith("resp") or head in {"r"}


def test_no_bare_body_reads():
    """Every HTTP body read anywhere must be capped (N2 class).

    Scope note: app/bounded_io.py IS the chokepoint and calls ``stream.read(n)``
    (a sized read on a ``stream`` receiver) -- exempt by receiver, not path, so
    it survives a tree-wide sweep. Widening kept precise on the response-object
    convention to avoid flagging file/socket reads.
    """
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED | {"bounded_io.py"}):
        offenders.extend(_bare_body_reads(tree, path))
    assert not offenders, "Use app.bounded_io.read_capped_sync/read_capped_async: " + ", ".join(offenders)


def _scope_matcher_definitions() -> list[str]:
    """File labels of every ``def scope_allows``/``_scope_allows`` under app/.

    M10: the bounded scope check was verbatim-copied into engine.py and
    recursive.py; a one-sided fix would have left the other on the stale
    contract. Deduped into app/crawl/scope_match.py -- a second definition
    anywhere means the copy crept back.
    """
    defs: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "scope_allows",
                "_scope_allows",
            }:
                defs.append(offender(path, node.lineno))
    return defs


def test_scope_matcher_has_exactly_one_definition():
    """The fail-closed scope matcher must live in one module only.

    Behavior parity across the dedupe is covered by test_scope_regex.py;
    this pins the structural property that no second copy exists to drift.
    """
    defs = _scope_matcher_definitions()
    assert len(defs) == 1, f"scope matcher duplicated again: {defs}"
    assert "app/crawl/scope_match.py" in defs[0], defs


def test_no_pinned_url_leaks_into_final_url():
    """A pinned fetch must never surface str(response.url) as final_url.

    _build_pinned_request_async rewrites a plain-HTTP host to its resolved IP
    so httpx reports the IP; surfacing it in final_url leaks topology and
    keys the cookie jar by IP. The fetchers must use current_url instead.
    Tree-wide: any module may grow a fetcher, so the final_url sink is
    checked everywhere (search providers use response.url legitimately but
    never as a final_url, so they are not false positives).
    """
    offenders = []
    for path, tree in app_trees():
        for node in ast.walk(tree):
            # final_url = str(response.url)  (assignment)
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name) and t.id == "final_url"]
                if targets and _is_response_url(node.value):
                    offenders.append(offender(path, node.lineno, "final_url = str(response.url)"))
            # HttpFetchResult(final_url=str(response.url))  (keyword)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "final_url" and _is_response_url(kw.value):
                        offenders.append(offender(path, node.lineno, "final_url=str(response.url)"))
    assert not offenders, "pinned URL leaked into final_url: " + ", ".join(offenders)


def _is_response_url(node: ast.expr) -> bool:
    """True for str(response.url) -- a response object's url coerced to str."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "url"
        and isinstance(node.args[0].value, ast.Name)
        and node.args[0].value.id == "response"
    )


def _unpinned_safe_url_parses(tree, path):
    """Every SafeUrl.parse fetch consumer must pass an explicit ``pin=``.

    An unpinned parse is the H5 rebinding window: validation resolves one IP,
    then ``connect`` re-resolves at open time and a hostile DNS can swap the
    answer. Forcing an explicit pin at every call site makes "forgot to pin"
    impossible to merge; safe_url.py itself is exempt (redirect_hop passes a
    computed pin preference, and the classmethod defines the parameter).
    """
    offenders = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "parse"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "SafeUrl"
        ):
            continue
        if not any(kw.arg == "pin" for kw in node.keywords):
            offenders.append(offender(path, node.lineno, "SafeUrl.parse(...) must pass pin=True"))
    return offenders


def test_safe_url_parse_always_pins():
    offenders = []
    for path, tree in app_trees(exclude=SANCTIONED | {"safe_url.py"}):
        offenders.extend(_unpinned_safe_url_parses(tree, path))
    assert not offenders, "unpinned SafeUrl.parse (DNS-rebinding window): " + "; ".join(offenders)


def test_no_inline_pinned_url_composition():
    """The pinned connect URL -- IP literal, IPv6 bracketing, port, and Host
    header composition -- is SafeUrl.connect()'s job alone.

    Three divergent copies of that composition had already drifted into the
    tree (safe_url, http fetcher, webhook builder); the copies disagreed on
    Host-port handling and all lacked IPv6 bracketing (H5-C). An f-string
    that mentions pinned_ip outside app/safe_url.py re-opens that drift.
    """
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset({"safe_url.py", "safe_regex.py"})):
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    if "pinned_ip" in value.value:
                        offenders.append(offender(path, node.lineno, "f-string with pinned_ip"))
                if isinstance(value, ast.FormattedValue):
                    expr = value.value
                    if isinstance(expr, ast.Attribute) and expr.attr == "pinned_ip":
                        offenders.append(offender(path, node.lineno, "f-string interpolates .pinned_ip"))
                    if (
                        isinstance(expr, ast.Name)
                        and isinstance(expr.ctx, ast.Load)
                        and "pinned" in expr.id.lower()
                    ):
                        offenders.append(offender(path, node.lineno, f"f-string interpolates {expr.id}"))
    assert not offenders, "compose pinned URLs via SafeUrl.connect(), never inline:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Frontier raw-href sinks (H1 class)
# ---------------------------------------------------------------------------


def _url_key_reads(node: ast.AST) -> bool:
    """True if the subtree reads dict key ``"url"`` (the unvalidated raw href)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "get"
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and sub.args[0].value == "url"
            ):
                return True
        elif isinstance(sub, ast.Subscript):
            if isinstance(sub.slice, ast.Constant) and sub.slice.value == "url":
                return True
    return False


def _frontier_sink_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``*.frontier.append(x)`` where ``x`` (or its same-function binding)
    reads the raw ``"url"`` key instead of going through frontier_link_url()."""
    offenders: list[str] = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
        tainted: set[str] = set()
        for assign in (a for a in ast.walk(fn) if isinstance(a, ast.Assign | ast.AnnAssign)):
            value = assign.value
            if value is None or _frontier_link_url_call(value):
                continue
            if _url_key_reads(value):
                targets = [assign.target] if isinstance(assign, ast.AnnAssign) else assign.targets
                for t in targets:
                    if isinstance(t, ast.Name):
                        tainted.add(t.id)
        for call in (c for c in ast.walk(fn) if isinstance(c, ast.Call)):
            f = call.func
            if (
                isinstance(f, ast.Attribute)
                and f.attr in ("append", "appendleft", "extend")
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "frontier"
            ):
                for arg in call.args:
                    if _url_key_reads(arg) or (isinstance(arg, ast.Name) and arg.id in tainted):
                        offenders.append(offender(path, call.lineno, f"raw href into frontier.{f.attr}"))
    return offenders


def _frontier_link_url_call(node: ast.AST) -> bool:
    """True if the subtree's root or any call node is frontier_link_url(...)."""
    return any(
        isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "frontier_link_url"
        for sub in ast.walk(node)
    )


def test_no_raw_href_frontier_sinks():
    """The frontier only ever carries frontier_link_url()-validated URLs (H1)."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_frontier_sink_offenders(tree, path))
    assert not offenders, "Read normalized_url via frontier_link_url(), never raw url: " + ", ".join(
        offenders
    )


def test_raw_href_frontier_detector_bites():
    """Prove the detector fires on the historical H1 shape."""
    bad = ast.parse(
        "async def _run(job, item):\n"
        "    for link in item.get('discovered_links') or []:\n"
        "        link_url = link.get('url') if isinstance(link, dict) else str(link)\n"
        "        if link_url:\n"
        "            job.frontier.append(link_url)\n"
    )
    offenders = _frontier_sink_offenders(bad, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 1 and "frontier.append" in offenders[0], offenders


# ---------------------------------------------------------------------------
# Crawl-response shaping chokepoint (jscpd #1 class)
# ---------------------------------------------------------------------------


def _crawl_response_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag explicit ``UnifiedCrawlResponse(..., results=[...])`` construction
    outside response_shaping — callers must use build_unified_crawl_response so
    persisted/webhook/REST payloads cannot drift (the #1754 rule, endpoint-wide).
    ``**stored`` deserialization splats have no explicit results= keyword."""
    if path.name == "response_shaping.py":
        return []
    offenders: list[str] = []
    for node, _ in call_keyword_values(
        tree,
        function_names="UnifiedCrawlResponse",
        keyword_names="results",
    ):
        offenders.append(offender(path, node.lineno, "explicit UnifiedCrawlResponse(results=...) build"))
    return offenders


def test_crawl_response_built_only_by_shaping_chokepoint():
    """Every UnifiedCrawlResponse-with-results flows through the shaper."""
    offenders = app_offenders(_crawl_response_construction_offenders)
    assert not offenders, "Use app.api.response_shaping.build_unified_crawl_response: " + ", ".join(offenders)


def test_crawl_response_chokepoint_detector_bites():
    """Synthetic bypass fires; **stored deserialization splat stays clean."""
    bypass = ast.parse("def f(results):\n    return UnifiedCrawlResponse(request_id='r', results=results)\n")
    offenders = _crawl_response_construction_offenders(bypass, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    stored = ast.parse("def f(stored):\n    return UnifiedCrawlResponse(**stored)\n")
    assert not _crawl_response_construction_offenders(stored, REPO_ROOT / "app" / "api" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Render-options construction chokepoint (C2/M11)
# ---------------------------------------------------------------------------


def _render_options_construction_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``RenderOptions(...)`` construction outside the resolver (C2/M11).

    The effective render options were once hand-assembled at every entry
    point and the copies drifted -- the browser strategy hardcoded the
    capture knobs while policy reads respected the caller elsewhere, and
    the /v1/render builder silently dropped browser_profile. Only
    app/crawl/render_options.py may decide the shape; callers map their own
    source into the resolver's mappings."""
    if path.name == "render_options.py":
        return []
    offenders: list[str] = []
    for node in calls_named(tree, "RenderOptions"):
        offenders.append(
            offender(path, node.lineno, "RenderOptions(...) built outside resolve_render_options")
        )
    return offenders


def test_render_options_built_only_by_resolver():
    """Every effective RenderOptions flows through resolve_render_options."""
    offenders = app_offenders(_render_options_construction_offenders)
    assert not offenders, (
        "Use app.crawl.render_options.resolve_render_options so request-side "
        "options and server defaults merge in exactly one place: " + ", ".join(offenders)
    )


def test_render_options_chokepoint_detector_bites():
    """Synthetic bypass fires; the resolver's own construction stays clean."""
    bypass = ast.parse("def f(policy):\n    return RenderOptions(timeout_seconds=policy['t'], include_html=True)\n")
    offenders = _render_options_construction_offenders(bypass, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "resolve_render_options" in offenders[0], offenders
    resolver = ast.parse("def f(request, defaults):\n    return RenderOptions(**kwargs)\n")
    assert not _render_options_construction_offenders(
        resolver, REPO_ROOT / "app" / "crawl" / "render_options.py"
    )


# ---------------------------------------------------------------------------
# ContentCache mirror twins (jscpd #2 class)
# ---------------------------------------------------------------------------

_CACHE_TWIN_METHODS = {"get", "aget", "set", "aset"}


def _memory_cache_twin_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag public cache twin methods touching ``_memory_cache`` directly.

    The RAM mirror's probe/store logic must live in the _*_locked helpers so
    the sync and async paths cannot drift (aget once lost the LRU count cap,
    and the pre-write mirror never rolled back — both invisible twins)."""
    offenders: list[str] = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
        if fn.name not in _CACHE_TWIN_METHODS:
            continue
        for sub in ast.walk(fn):
            if (
                isinstance(sub, ast.Subscript)
                and isinstance(sub.value, ast.Attribute)
                and sub.value.attr == "_memory_cache"
            ):
                offenders.append(
                    offender(path, fn.lineno, f"{fn.name}() reads/writes _memory_cache directly")
                )
                break
    return offenders


def test_memory_cache_only_via_locked_helpers():
    """get/set/aget/aset never touch the raw RAM-mirror dict."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_memory_cache_twin_offenders(tree, path))
    assert not offenders, "Use _take_memory_locked/_remember_memory_locked: " + ", ".join(offenders)


def test_memory_cache_twin_detector_bites():
    """Synthetic drifted twin fires; helper-shaped code stays clean."""
    bad = ast.parse(
        "class C:\n    async def aget(self, key):\n        with self._lock:\n"
        "            if key in self._memory_cache:\n"
        "                return self._memory_cache[key][1]\n"
    )
    offenders = _memory_cache_twin_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "aget" in offenders[0], offenders
    good = ast.parse(
        "class C:\n    async def aget(self, key):\n        return self._take_memory_locked(key)\n"
    )
    assert not _memory_cache_twin_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Lock ownership: a *_locked helper presumes the caller holds the lock (M1)
# ---------------------------------------------------------------------------


def _is_locked_helper_name(name: str | None) -> bool:
    return bool(name) and name.startswith("_") and name.endswith("_locked")


def _references_lock(expr: ast.expr) -> bool:
    """True for ``self._lock`` / ``some_lock`` style context expressions."""
    target = expr.func if isinstance(expr, ast.Call) else expr
    if isinstance(target, ast.Attribute):
        return "lock" in target.attr.lower()
    if isinstance(target, ast.Name):
        return "lock" in target.id.lower()
    return False


def _locked_calls_outside_lock_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a call to a ``_*_locked`` helper not lexically inside a lock ``with``.

    The ``_*_locked`` cache helpers assume the caller already holds ``self._lock``
    (they touch shared index/RAM state with no internal acquisition). ``get()``
    once called ``_remember_memory_locked`` off-lock while ``aget()`` held it —
    the exact sync/async drift the twin detector can't see, because the call site
    is helper-shaped and only the *lock ownership* was wrong. This closes that by
    making an unlocked ``_*_locked`` call a hard failure anywhere in ``app``.
    """
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if not _is_locked_helper_name(fname):
            continue
        under_lock = False
        cur: ast.AST | None = parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.With, ast.AsyncWith)) and any(
                _references_lock(item.context_expr) for item in cur.items
            ):
                under_lock = True
                break
            cur = parents.get(cur)
        if not under_lock:
            offenders.append(offender(path, node.lineno, f"{fname}() called outside a lock context manager"))
    return offenders


def test_locked_cache_helpers_are_called_under_the_lock():
    """M1 class: every ``_*_locked`` call sits inside a ``with ...lock`` block."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_locked_calls_outside_lock_offenders(tree, path))
    assert not offenders, "call *_locked helpers inside `with self._lock`: " + ", ".join(offenders)


def test_locked_helper_lock_ownership_detector_bites():
    """An off-lock *_locked call fires; the guarded form stays clean."""
    bad = ast.parse("class C:\n    def get(self, key):\n        return self._remember_memory_locked(key)\n")
    offenders = _locked_calls_outside_lock_offenders(bad, REPO_ROOT / "app" / "_synthetic.py")
    assert len(offenders) == 1 and "_remember_memory_locked" in offenders[0], offenders
    good = ast.parse(
        "class C:\n    def get(self, key):\n        with self._lock:\n            return self._remember_memory_locked(key)\n"
    )
    assert not _locked_calls_outside_lock_offenders(good, REPO_ROOT / "app" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Error-envelope shape (jscpd #3 class)
# ---------------------------------------------------------------------------

# app/api/exceptions.py owns the envelope; it is the sanctioned definition
# (exclude matches by file *name*, matching app_trees' own semantics).
ENVELOPE_SANCTIONED = SANCTIONED | {"exceptions.py"}


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _envelope_dict_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag hand-rolled error envelopes; the factory is the only definition.

    Three shapes: ANY ``HTTPException(...)`` construction (M8: the wire shape
    may only be built by :func:`http_error`/``browser_pool_unavailable`` -- a
    bare ``HTTPException(detail="str")`` reaches the handler's fallback and
    used to be laundered into a fabricated ``http_<status>`` code); a
    ``HTTPException(detail={...code, message...})`` literal (the pre-factory
    form); and any dict carrying all three envelope keys anywhere (catches
    JSONResponse/return-value variants).
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if func_name == "HTTPException":
                if node.keywords and any(
                    kw.arg == "detail"
                    and isinstance(kw.value, ast.Dict)
                    and {"code", "message"} <= _dict_keys(kw.value)
                    for kw in node.keywords
                ):
                    offenders.append(offender(path, node.lineno, "HTTPException detail= envelope literal"))
                else:
                    offenders.append(offender(path, node.lineno, "bare HTTPException construction"))
        elif isinstance(node, ast.Dict) and {"code", "message", "details"} <= _dict_keys(node):
            offenders.append(offender(path, node.lineno, "error envelope dict literal"))
    return offenders


def test_no_hand_rolled_error_envelopes():
    """Every HTTP error body flows through error_envelope/http_error (#1722)."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=ENVELOPE_SANCTIONED):
        offenders.extend(_envelope_dict_offenders(tree, path))
    assert not offenders, "Use app.api.exceptions.http_error/browser_pool_unavailable: " + ", ".join(
        offenders
    )


def test_envelope_detector_bites():
    """All three drift shapes fire; factory-shaped code stays clean."""
    bad = ast.parse(
        "raise HTTPException(status_code=404, detail={'code': 'not_found', 'message': 'gone'})\n"
        "response = {'code': 'pending', 'message': 'm', 'details': {}}\n"
        "raise HTTPException(status_code=404, detail='plain string')\n"
        "raise fastapi.HTTPException(status_code=500)\n"
    )
    offenders = _envelope_dict_offenders(bad, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 4, offenders
    good = ast.parse(
        "from app.api.exceptions import http_error\n"
        "raise http_error(404, 'not_found', 'gone')\n"
        "response = build_thing({'code': 1, 'message': 'x'})\n"
    )
    assert not _envelope_dict_offenders(good, REPO_ROOT / "app" / "api" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Search-provider result normalization (jscpd #4 family)
# ---------------------------------------------------------------------------


def _provider_loop_reinlines(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the keyed-provider dedupe loop re-inlined outside the collector.

    The exact copy-paste shape is ``seen: set`` + ``isinstance(item, dict)``
    guard + ``SearchResult`` construction in one function. DuckDuckGo's
    provider legitimately keeps a separate lxml node-walk: it has no dict
    guard, so the sink never fires on it.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.name == "_collect_results":
            continue
        subs = list(ast.walk(node))
        assigns_seen = any(
            isinstance(s, ast.AnnAssign) and getattr(s.target, "id", "") == "seen" for s in subs
        )
        builds_result = any(
            isinstance(s, ast.Call) and getattr(s.func, "id", "") == "SearchResult" for s in subs
        )
        guards_dict = any(
            isinstance(s, ast.Call)
            and getattr(s.func, "id", "") == "isinstance"
            and len(s.args) >= 2
            and getattr(s.args[1], "id", "") == "dict"
            for s in subs
        )
        if assigns_seen and builds_result and guards_dict:
            offenders.append(offender(path, node.lineno, "dedupe loop re-inlined outside _collect_results"))
    return offenders


def test_no_provider_dedupe_loop_reinlines():
    """One normalize/dedupe/clip contract for all keyed providers."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_provider_loop_reinlines(tree, path))
    assert not offenders, "Use app.search.providers._collect_results: " + ", ".join(offenders)


def test_provider_loop_detector_bites():
    """The three-provider copy fires; the DDG lxml-walk shape stays clean."""
    bad = ast.parse(
        "async def search(self, query, limit, timeout_seconds):\n"
        "    results = []\n"
        "    seen: set[str] = set()\n"
        "    for item in payload.get('results') or []:\n"
        "        if not isinstance(item, dict):\n"
        "            continue\n"
        "        results.append(SearchResult(url=item.get('url')))\n"
        "    return results\n"
    )
    offenders = _provider_loop_reinlines(bad, REPO_ROOT / "app" / "search" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    ddg_like = ast.parse(
        "async def search(self, query, limit, timeout_seconds):\n"
        "    results = []\n"
        "    seen: set[str] = set()\n"
        "    for node in tree.xpath('//div'):\n"
        "        href = node.get('href')\n"
        "        results.append(SearchResult(url=href))\n"
        "    return results\n"
    )
    assert not _provider_loop_reinlines(ddg_like, REPO_ROOT / "app" / "search" / "_synthetic.py")


# ---------------------------------------------------------------------------
# robots.txt scanning (jscpd #5 family)
# ---------------------------------------------------------------------------

_ROBOTS_SCANNER_SANCTIONED = "iter_robots_fields"


def _robots_scanner_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """One line-walk for every robots consumer.

    Sink: a function doing the ``lstrip(BOM) + splitlines()`` scan. It lived
    three times (crawl-delay, rules, sitemaps) and drifted on the empty-value
    gate; the tokenizer in app/crawl/robots_parse.py is now the only one.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name == _ROBOTS_SCANNER_SANCTIONED:
            continue
        has_bom_strip = any(
            isinstance(s, ast.Call)
            and getattr(s.func, "attr", "") == "lstrip"
            and s.args
            and isinstance(s.args[0], ast.Constant)
            and s.args[0].value in ("﻿", "\ufeff")
            for s in ast.walk(node)
        )
        has_split = any(
            isinstance(s, ast.Call) and getattr(s.func, "attr", "") == "splitlines" for s in ast.walk(node)
        )
        if has_bom_strip and has_split:
            offenders.append(offender(path, node.lineno, f"{node.name} re-rolls the robots line-scanner"))
    return offenders


def test_robots_line_scanner_lives_in_one_place():
    """Every robots consumer walks lines through the shared tokenizer."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_robots_scanner_offenders(tree, path))
    assert not offenders, "app/crawl/robots_parse.iter_robots_fields is the single scanner: " + ", ".join(
        offenders
    )


def test_robots_scanner_detector_bites():
    bad = ast.parse(
        "def parse_robots_rules(text):\n"
        "    for raw in text.lstrip('\\ufeff').splitlines():\n"
        "        yield raw\n"
    )
    offenders = _robots_scanner_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "parse_robots_rules" in offenders[0], offenders
    good = ast.parse(
        "def iter_robots_fields(text):\n"
        "    for raw in text.lstrip('\\ufeff').splitlines():\n"
        "        yield raw.strip()\n"
    )
    assert not _robots_scanner_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Test-fake construction (jscpd #6: sanctioned builders over hand-built fakes)
# ---------------------------------------------------------------------------

_FAKE_BUILDER_SANCTIONED = frozenset({"_testkit.py"})


def _testkit_builder_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a test file hand-building a model the testkit already fabricates.

    HttpFetchResult's field set is the crawl contract; a fixture that spells
    it out by hand silently diverges when the contract grows (the exact
    drift #1797 is about). _testkit.make_fetch is the sanctioned builder.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "HttpFetchResult":
            offenders.append(
                offender(path, node.lineno, "use _testkit.make_fetch instead of HttpFetchResult(...)")
            )
    return offenders


def test_tests_do_not_hand_build_http_fetch_results():
    """Every fetch fake comes from the one sanctioned builder."""
    offenders: list[str] = []
    for path, tree in trees_under(TESTS_DIR):
        if path.name in _FAKE_BUILDER_SANCTIONED:
            continue
        offenders.extend(_testkit_builder_offenders(tree, path))
    assert not offenders, "Use _testkit.make_fetch(): " + ", ".join(offenders)


def test_fetch_builder_detector_bites():
    bad = ast.parse("def f():\n    return HttpFetchResult(url='u')\n")
    offenders = _testkit_builder_offenders(bad, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    good = ast.parse("def f():\n    return make_fetch(url='u')\n")
    assert not _testkit_builder_offenders(good, REPO_ROOT / "tests" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Poll-loop reimplementation (jscpd test-side drift, sibling to the fake
# builder finding): _testkit.poll_until is the sanctioned predicate poller.
# A hand-rolled ``while True`` poller that awaits ``asyncio.wait_for`` without
# a ``pytest.fail`` termination silently returns False on timeout, diverging
# from the one that names the failing condition. Flag the shape.
# ---------------------------------------------------------------------------


def _call_node_names(node: ast.AST) -> set[str]:
    """Names of every call target under ``node`` (e.g. ``pytest.fail``)."""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            try:
                names.add(ast.unparse(child.func))
            except Exception:
                pass
    return names


def _poll_loop_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        src = ast.dump(node)
        has_while_true = any(
            isinstance(n, ast.While) and ast.unparse(n.test) == "True" for n in ast.walk(node)
        )
        if not (has_while_true and "iscoroutine" in src and "wait_for" in src):
            continue
        # The sanctioned _testkit.poll_until terminates with pytest.fail. A
        # docstring may *claim* a fail; only a real call node counts.
        if any(name == "fail" or name.endswith(".fail") for name in _call_node_names(node)):
            continue
        offenders.append(
            offender(path, node.lineno, "use _testkit.poll_until instead of a hand-rolled poll loop")
        )
    return offenders


def test_tests_do_not_relinline_poll_loop():
    """One sanctioned poller; tests must not re-roll their own."""
    offenders: list[str] = []
    for path, tree in trees_under(TESTS_DIR):
        if path.name in _FAKE_BUILDER_SANCTIONED:
            continue
        offenders.extend(_poll_loop_offenders(tree, path))
    assert not offenders, "Use _testkit.poll_until: " + "; ".join(offenders)


def test_poll_loop_detector_bites():
    bad = ast.parse(
        "async def _poll_until(pred, *, timeout=10.0):\n"
        "    async def _runner():\n"
        "        while True:\n"
        "            v = pred()\n"
        "            if asyncio.iscoroutine(v):\n"
        "                v = await v\n"
        "            if v:\n"
        "                return True\n"
        "    return await asyncio.wait_for(_runner(), timeout=timeout)\n"
    )
    offenders = _poll_loop_offenders(bad, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    good = ast.parse(
        "async def poll_until(pred, *, timeout=10.0, message=None):\n"
        "    async def _runner():\n"
        "        while True:\n"
        "            if pred():\n"
        "                return True\n"
        "    await asyncio.wait_for(_runner(), timeout=timeout)\n"
        "    pytest.fail(message)\n"
    )
    assert not _poll_loop_offenders(good, REPO_ROOT / "tests" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Decompression bomb guard (jscpd #5 family / H2 closure)
# ---------------------------------------------------------------------------

_DECOMPRESS_FUNCS = frozenset({"decompress_body", "_decompress_body"})
_DECOMPRESS_SANCTIONED = frozenset({"decompression.py"})


def _decompress_cap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a decompress_body call that omits the mandatory output cap.

    A small gzip/deflate/brotli stream can expand to gigabytes; every
    production caller must bound the output. The cap is now a required
    keyword at the chokepoint, so this AST sweep is belt-and-suspenders
    against a caller reaching for an ``int`` default or ``**kwargs``.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if fname not in _DECOMPRESS_FUNCS:
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        if "max_output_bytes" not in kwargs:
            offenders.append(offender(path, node.lineno, f"{fname}(...) without max_output_bytes"))
    return offenders


def test_all_decompress_calls_pass_a_cap():
    """Every production decompression is size-bounded (H2 closure)."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED | _DECOMPRESS_SANCTIONED):
        offenders.extend(_decompress_cap_offenders(tree, path))
    assert not offenders, "decompress_body needs max_output_bytes=: " + ", ".join(offenders)


def test_decompress_cap_detector_bites():
    bad = ast.parse("def f(raw, enc):\n    return decompress_body(raw, enc)\n")
    offenders = _decompress_cap_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    good = ast.parse("def f(raw, enc):\n    return decompress_body(raw, enc, max_output_bytes=1024)\n")
    assert not _decompress_cap_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Decompression reaches only through the capped chokepoint (legacy #3)
# ---------------------------------------------------------------------------

# The codecs app/crawl/decompression.py imports to implement decompress_body.
_DECOMPRESS_CODECS = frozenset({"gzip", "zlib", "brotli"})
_DECOMPRESS_CODEC_METHODS = frozenset({"decompress", "decompressobj", "GzipFile", "Decompressor"})


def _decompress_bypass_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag raw stdlib decompression (import or call) outside the chokepoint.

    All body decoding must route through ``decompression.decompress_body``, which
    makes ``max_output_bytes`` mandatory and returns input unchanged on decoder
    error (so a forged ``Content-Encoding`` cannot expand or corrupt unbounded).
    A module that imports or calls ``gzip``/``zlib``/``brotli`` directly can
    re-introduce the header-trusting, uncapped decompress the legacy cleanup
    removed — the ``_decompress_cap`` sweep only guards the chokepoint's own
    callers, so this closes the bypass around it.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in _DECOMPRESS_CODECS:
                    offenders.append(
                        offender(
                            path,
                            node.lineno,
                            f"imports decompression codec '{alias.name}' (bypasses decompress_body)",
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in _DECOMPRESS_CODECS:
                offenders.append(
                    offender(
                        path,
                        node.lineno,
                        f"imports decompression codec from '{node.module}' (bypasses decompress_body)",
                    )
                )
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in _DECOMPRESS_CODECS
                and func.attr in _DECOMPRESS_CODEC_METHODS
            ):
                offenders.append(
                    offender(
                        path,
                        node.lineno,
                        f"calls {func.value.id}.{func.attr}() directly (bypasses decompress_body)",
                    )
                )
    return offenders


def test_decompression_reaches_only_through_the_chokepoint():
    """No app module touches gzip/zlib/brotli except the decompression chokepoint."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED | _DECOMPRESS_SANCTIONED):
        offenders.extend(_decompress_bypass_offenders(tree, path))
    assert not offenders, "decompress only via decompression.decompress_body(): " + ", ".join(offenders)


def test_decompress_bypass_detector_bites():
    """A header-driven raw decompress fires on both import and call."""
    bad = ast.parse(
        "import gzip\n"
        "def f(raw, hdr):\n"
        "    if 'gzip' in hdr.get('content-encoding', ''):\n"
        "        return gzip.decompress(raw)\n"
    )
    offenders = _decompress_bypass_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 2, offenders
    good = ast.parse(
        "from app.crawl.decompression import decompress_body\n"
        "def f(raw, hdr):\n"
        "    return decompress_body(raw, hdr.get('content-encoding', ''), max_output_bytes=1024)\n"
    )
    assert not _decompress_bypass_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Handle minting is single-sourced (H4 residue / HIGH #9 entropy policy)
# ---------------------------------------------------------------------------

_ID_MINT_SANCTIONED = frozenset({"ids.py"})


def _id_mint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct ``uuid4()`` calls or ``.hex`` slicing outside app/crawl/ids.

    Handles double as the lookup credential for status/result routes, so the
    full-entropy policy must live in exactly one primitive; a re-inlined mint
    is where a shorter slice silently regresses to 48 bits.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname == "uuid4":
                offenders.append(offender(path, node.lineno, "uuid4() minted outside app/crawl/ids.py"))
        elif isinstance(node, ast.Subscript) and getattr(node.value, "attr", None) == "hex":
            offenders.append(offender(path, node.lineno, "uuid hex sliced outside app/crawl/ids.py"))
    return offenders


def test_handles_are_minted_in_one_place():
    """Only app/crawl/ids.py calls uuid4 or slices its hex (H4 residue)."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED | _ID_MINT_SANCTIONED):
        offenders.extend(_id_mint_offenders(tree, path))
    assert not offenders, "handle minting is single-sourced in app/crawl/ids.py: " + ", ".join(offenders)


def test_id_mint_detector_bites():
    bad = ast.parse("import uuid\n\ndef f():\n    return uuid.uuid4().hex[:12]\n")
    offenders = _id_mint_offenders(bad, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 2, offenders  # uuid4 call AND hex slice
    good = ast.parse("from app.crawl.ids import new_request_id\n\ndef f():\n    return new_request_id()\n")
    assert not _id_mint_offenders(good, REPO_ROOT / "app" / "api" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Header ints are guarded (L2): a hostile header must never crash a fetch
# ---------------------------------------------------------------------------


def _is_int_or_float_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"int", "float"}


def _reads_headers(node: ast.AST) -> bool:
    """True for ``x.headers.get(...)`` or ``x.headers[...]`` -- a raw header read."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        source = node.func.value
        return node.func.attr == "get" and isinstance(source, ast.Attribute) and source.attr == "headers"
    if isinstance(node, ast.Subscript):
        return isinstance(node.value, ast.Attribute) and node.value.attr == "headers"
    return False


def _catches_value_error(node: ast.Try) -> bool:
    """True if any handler catches ValueError (incl. via a tuple/Exception)."""
    for handler in node.handlers:
        exc = handler.type
        names = (
            {elt.id for elt in exc.elts if isinstance(elt, ast.Name)}
            if isinstance(exc, ast.Tuple)
            else {exc.id}
            if isinstance(exc, ast.Name)
            else set()
        )
        if names & {"ValueError", "Exception", "BaseException"}:
            return True
    return False


def _unguarded_header_int_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``int``/``float`` of a header-derived value outside a try/except ValueError.

    Parsing a ``Content-Length`` (or any header) into a number without guarding
    the conversion is the L2 class: one crafted ``Content-Length: abc`` raises
    out of the fetcher and fails the whole request. The safe shapes either go
    through a guard predicate (the arg is a parameter, not an in-scope header
    read) or wrap the ``int()`` in ``try/except ValueError`` (middleware's
    request-cap check). Tree-wide because any module can grow a header read.
    """
    offenders: list[str] = []
    for func in _functions_with(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        header_names = {
            target.id
            for node in ast.walk(func)
            if isinstance(node, ast.Assign) and _reads_headers(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        guarded = {
            id(sub)
            for node in ast.walk(func)
            if isinstance(node, ast.Try) and _catches_value_error(node)
            for sub in ast.walk(node)
            if _is_int_or_float_call(sub)
        }
        for node in ast.walk(func):
            if not _is_int_or_float_call(node) or id(node) in guarded or not node.args:
                continue
            arg = node.args[0]
            is_derived = _reads_headers(arg) or (isinstance(arg, ast.Name) and arg.id in header_names)
            if is_derived:
                offenders.append(
                    offender(path, node.lineno, f"{func.name}() parses a header number unguarded")
                )
    return offenders


def test_header_numbers_are_converted_under_guard():
    """No unguarded int()/float() on a header value anywhere in app/ (L2)."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_unguarded_header_int_offenders(tree, path))
    assert not offenders, "parse header numbers through a guard or try/except ValueError: " + ", ".join(
        offenders
    )


def test_header_int_detector_bites():
    bad = ast.parse(
        "async def fetcher(resp):\n    content_length = resp.headers.get('content-length')\n"
        "    return int(content_length) > 100\n"
    )
    offenders = _unguarded_header_int_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    inline_bad = ast.parse(
        "async def fetcher(resp):\n    return int(resp.headers.get('content-length', 0)) > 100\n"
    )
    assert _unguarded_header_int_offenders(inline_bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    guarded = ast.parse(
        "def guard(value):\n    try:\n        return int(value)\n    except ValueError:\n        return 0\n"
    )
    assert not _unguarded_header_int_offenders(guarded, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # A sanitizer takes a parameter, not a live header read -- never flagged.
    param_based = ast.parse(
        "def _declared_length_exceeds(header_value, limit):\n    return int(header_value) > limit\n"
    )
    assert not _unguarded_header_int_offenders(param_based, REPO_ROOT / "app" / "crawl" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Robots politeness floor is committed only through the bounded setter (M1)
# ---------------------------------------------------------------------------


def _unbounded_domain_delay_writes(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct writes to ``_domain_min_delay`` outside ``set_domain_min_delay``.

    The robots Crawl-delay floor is clamped to a finite politeness ceiling only
    in ``set_domain_min_delay``; a bare ``self._domain_min_delay[...] = ...``
    anywhere else (even ``+=``) bypasses that bound and can drop an
    ``inf``/huge value straight into ``asyncio.sleep`` (the M1 hang).
    """
    offenders: list[str] = []
    for func in (
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if func.name == "set_domain_min_delay":
            continue
        for node in ast.walk(func):
            if not isinstance(node, (ast.Assign, ast.AugAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "_domain_min_delay"
                ):
                    offenders.append(
                        offender(path, node.lineno, f"unbounded write to _domain_min_delay in {func.name}()")
                    )
    return offenders


def test_domain_politeness_floor_is_committed_only_via_the_setter():
    """Only set_domain_min_delay writes _domain_min_delay (M1 chokepoint)."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_unbounded_domain_delay_writes(tree, path))
    assert not offenders, (
        "commit the Crawl-delay floor through the clamped set_domain_min_delay: " + ", ".join(offenders)
    )


def test_domain_delay_detector_bites():
    bad = ast.parse(
        "class S:\n    def other(self):\n        self._domain_min_delay['x'] = 1e9\n"
        "    def bump(self):\n        self._domain_min_delay['y'] += 1\n"
    )
    offenders = _unbounded_domain_delay_writes(bad, REPO_ROOT / "app" / "_synthetic.py")
    assert len(offenders) == 2, offenders
    good = ast.parse(
        "class S:\n    def set_domain_min_delay(self, d, s):\n        self._domain_min_delay[d] = min(s, 300.0)\n"
        "    def read(self, d):\n        return self._domain_min_delay.get(d, 0.0)\n"
    )
    assert not _unbounded_domain_delay_writes(good, REPO_ROOT / "app" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Whole-site frontier must not drain on transient failure
# ---------------------------------------------------------------------------


def _ungated_job_visited_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag committing a *whole-site* URL to ``job.visited`` without the gate.

    A URL that fails for a transient reason (network/dns/tls/timeout/crash) must
    be re-queued, not marked visited: committing it is what let a network outage
    drain a whole-site frontier to a seed-only ``COMPLETED``. Keyed on the
    persisted ``job.visited`` receiver so the local per-request ``visited`` set
    (recursive/fetch_helpers) — a different thing, where retrying the same failed
    URL within one crawl is pointless — is not matched.
    """
    offenders: list[str] = []
    for func in _functions_with(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        commits = [
            n
            for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "add"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "visited"
            and isinstance(n.func.value.value, ast.Name)
            and n.func.value.value.id == "job"
        ]
        if not commits:
            continue
        gated = any(isinstance(n, ast.Name) and n.id == "TRANSIENT_CLASSIFICATIONS" for n in ast.walk(func))
        if not gated:
            offenders.append(
                offender(
                    path,
                    commits[0].lineno,
                    "job.visited.add() without the TRANSIENT_CLASSIFICATIONS re-queue gate",
                )
            )
    return offenders


def test_whole_site_visited_commit_is_transient_gated():
    """A transiently-failed whole-site URL is re-queued, never counted done."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_ungated_job_visited_offenders(tree, path))
    assert not offenders, (
        "gate every job.visited.add on TRANSIENT_CLASSIFICATIONS (re-queue transient failures): "
        + ", ".join(offenders)
    )


def test_whole_site_visited_gate_detector_bites():
    """An unguarded whole-site visited commit fires; the gated form stays clean."""
    bad = ast.parse("def run(job):\n    for url in batch:\n        job.visited.add(url)\n")
    offenders = _ungated_job_visited_offenders(bad, REPO_ROOT / "app" / "_synthetic.py")
    assert len(offenders) == 1 and "TRANSIENT_CLASSIFICATIONS" in offenders[0], offenders
    good = ast.parse(
        "def run(job, outcome, TRANSIENT_CLASSIFICATIONS):\n"
        "    for url in batch:\n"
        "        if outcome in TRANSIENT_CLASSIFICATIONS:\n"
        "            continue\n"
        "        job.visited.add(url)\n"
    )
    assert not _ungated_job_visited_offenders(good, REPO_ROOT / "app" / "_synthetic.py")


# ---------------------------------------------------------------------------
# Error-handling chokepoint drift detectors
#
# A strategy that raises (rather than returning a classified result) falls to
# the scheduler's blanket envelope and is misclassified, or in on_result mode
# silently drops the page. Three invariants keep that from drifting back:
#
#   1. _engine_result_from_exception is the only place a strategy exception may
#      become a result (no hand-rolled failed-result construction with a
#      hardcoded classification outside the two legitimate config guards).
#   2. BrowserPoolExhaustedError may only be caught by sanctioned sites
#      (the render endpoints, which own the 503 transport response) -- never
#      ad-hoc catch sites in the crawl path, which would re-derive retry
#      semantics locally instead of reading the classification.
#   3. _classify_error_code must be defined in result_helpers (neutral home),
#      never in a strategy module; strategies reach for it, they don't own it.
# ---------------------------------------------------------------------------

# Classifications that a hand-rolled _failed_engine_result/_make_engine_result
# may legitimately carry without going through the exception chokepoint:
#   browser_crash / unsupported_strategy: the engine's config guards -- there
#     is no exception to classify, the worker was never built
#   binary_no_text: a fetched page whose body has no extractable text
#   not_modified: a 304 cache hit (a *success*, built by the 304 path)
# Everything else means a real exception was converted to a result by hand,
# which is the drift the chokepoint exists to prevent.
_ERROR_CHOKEPOINT_SANCTIONED = frozenset({"result_helpers.py"})


_NON_EXCEPTION_CLASSIFICATIONS = frozenset(
    {"browser_crash", "unsupported_strategy", "binary_no_text", "not_modified"}
)


def _has_error_field(node, expected) -> bool:
    """The call carries the given taxonomy code in its ``error=`` kwarg."""
    return any(
        kw.arg == "error" and isinstance(kw.value, ast.Constant) and kw.value.value == expected
        for kw in node.keywords
    )


def _hand_rolled_failed_result_offenders(tree, path):
    """Failed-result construction with a hardcoded classification outside the
    chokepoint and the engine's two config guards (no browser worker / unknown
    strategy -- those have no exception to classify)."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_failed_engine_result", "_make_engine_result")):
            continue
        status = None
        for kw in node.keywords:
            if kw.arg == "classification" and isinstance(kw.value, ast.Constant):
                status = kw.value.value
        if status in (None,) or status in _NON_EXCEPTION_CLASSIFICATIONS:
            continue
        # The engine's unsupported-strategy guard and the scheduler's
        # circuit-breaker guard are policy decisions, not caught exceptions:
        # they carry their own taxonomy code in the error field.
        if status == "unknown_error" and _has_error_field(node, "unsupported_strategy"):
            continue
        if status == "network_error" and _has_error_field(node, "circuit_breaker_open"):
            continue
        yield offender(path, node.lineno, f"hand-rolled classification={status!r}; route through _engine_result_from_exception")


def test_no_hand_rolled_failed_results_outside_the_chokepoint():
    offenders = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_hand_rolled_failed_result_offenders(tree, path))
    assert not offenders, (
        "failed results with a hardcoded classification must go through "
        "_engine_result_from_exception (the single exception->result chokepoint): "
        + ", ".join(offenders)
    )


def test_hand_rolled_failed_result_detector_bites():
    bad = ast.parse(
        "def f():\n"
        "    return _failed_engine_result(\n"
        '        classification="network_error", error="worker_exception",\n'
        "    )\n"
    )
    offenders = list(_hand_rolled_failed_result_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "_engine_result_from_exception" in offenders[0], offenders
    good = ast.parse(
        "def f():\n"
        "    return _failed_engine_result(\n"
        '        classification="browser_crash",\n'
        "    )\n"
    )
    assert not list(_hand_rolled_failed_result_offenders(good, REPO_ROOT / "app" / "_synthetic.py"))


_POOL_EXHAUSTION_CATCH_SANCTIONED = frozenset({"render.py", "render_endpoint.py", "browser_pool.py"})


def _unsanctioned_pool_exhaustion_catch_offenders(tree, path):
    """A catch of BrowserPoolExhaustedError outside the render transports and
    the worker guard is a local re-derivation of retry semantics."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler) or node.type is None:
            continue
        names = []
        t = node.type
        if isinstance(t, ast.Name):
            names.append(t.id)
        elif isinstance(t, ast.Tuple):
            names.extend(e.id for e in t.elts if isinstance(e, ast.Name))
        if not any("BrowserPoolExhaustedError" in n for n in names):
            continue
        yield offender(path, node.lineno, "catching BrowserPoolExhaustedError; read the classification instead")


def test_pool_exhaustion_catch_sites_are_sanctioned():
    offenders = []
    for path, tree in app_trees(exclude=SANCTIONED | _POOL_EXHAUSTION_CATCH_SANCTIONED):
        offenders.extend(_unsanctioned_pool_exhaustion_catch_offenders(tree, path))
    assert not offenders, (
        "BrowserPoolExhaustedError may only be caught by the render transports "
        "(503 response) and the browser worker guard; the crawl path reads the "
        "browser_pool_exhausted classification: " + ", ".join(offenders)
    )


def test_pool_exhaustion_catch_detector_bites():
    bad = ast.parse(
        "async def f():\n"
        "    try:\n"
        "        await crawl()\n"
        "    except BrowserPoolExhaustedError:\n"
        "        return None\n"
    )
    offenders = list(_unsanctioned_pool_exhaustion_catch_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "classification" in offenders[0], offenders
    good = ast.parse(
        "async def f():\n"
        "    result = await crawl()\n"
        "    if result.classification == 'browser_pool_exhausted':\n"
        "        requeue()\n"
    )
    assert not list(_unsanctioned_pool_exhaustion_catch_offenders(good, REPO_ROOT / "app" / "_synthetic.py"))


def test_classifier_lives_in_the_neutral_home():
    """_classify_error_code must be defined in result_helpers, not re-defined
    in a strategy module (the old site, http.py, forced browser.py to import
    across strategies)."""
    for path, tree in app_trees(exclude=SANCTIONED | _ERROR_CHOKEPOINT_SANCTIONED):
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_classify_error_code"
            ):
                raise AssertionError(
                    offender(path, node.lineno, "_classify_error_code defined outside result_helpers")
                )


# ---------------------------------------------------------------------------
# Value-validity chokepoint (the meta-fix)
#
# The suite polices *routing* (a failed result must be built through the
# chokepoint primitives) but never *value validity*: nothing verified the
# classification string flowing through the primitive was a legal enum member.
# That is how url_budget_exceeded shipped -- a genuinely distinct signal passed
# as known_classification=, the lenient internal dataclass accepted it, and the
# strict enum at response shaping rejected the whole batch with a 500. The
# routing tests were green the entire time.
#
# This detector closes the class, not the instance: any classification handed
# to the primitives as a literal must be a CrawlClassification member.
# ---------------------------------------------------------------------------

_CLASSIFICATION_VALUE_SOURCES = ("classification", "known_classification")
_RESULT_PRIMITIVES = (
    # Derived, not transcribed: the per-field detectors that used to live here
    # matched on a six-name hand-kept list and silently omitted three live
    # primitives plus every direct dataclass construction. The generic sweep
    # in tests/membership_gate.py reads the registry from the models, so a
    # primitive added tomorrow is covered the day it lands. These tuples stay
    # only as the per-field detectors' public surface; the registry is the
    # source of truth and this is its shadow, kept in sync by the
    # subsumption assert below.
    "_failed_engine_result",
    "_make_engine_result",
    "_engine_result_from_exception",
    "_skipped_engine_result",
    "_crawl_result_from_exception",
    "_terminal_crawl_result",
)


def _result_primitive_names() -> frozenset[str]:
    """The registry the per-field detectors actually sweep.

    Routed through the derived registry rather than the transcribed tuple
       above so the three per-field detectors cannot re-acquire the coverage gap
    the generic sweep was written to close.
    """
    from membership_gate import RESULT_PRIMITIVES

    return RESULT_PRIMITIVES
def _non_member_classification_offenders(tree, path, *, members):
    """A literal classification passed to a result primitive must be a member.

    ``members`` is the live ``CrawlClassification`` value set. There is no
    hand-maintained exemption list: such a list is precisely how a new code
    ships as a non-member -- the list would have to be remembered and updated
    in lockstep with the enum, and it never is. A non-literal (variable or
    attribute access, e.g. CrawlClassification.x) is what callers should
    write, and passes without comment.
    """
    for node, value in call_keyword_values(
        tree,
        function_names=_result_primitive_names(),
        keyword_names=_CLASSIFICATION_VALUE_SOURCES,
    ):
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if value.value not in members:
            yield offender(
                path, node.lineno,
                f"classification={value.value!r} is not a CrawlClassification member; "
                f"add it to the enum (app/constants.py) or route through the classifier",
            )


def test_engine_emitted_classifications_are_enum_members():
    from app.constants import CrawlClassification

    members = {m.value for m in CrawlClassification}
    offenders = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_non_member_classification_offenders(tree, path, members=members))
    assert not offenders, (
        "a literal classification passed to a result primitive must be a "
        "CrawlClassification member, or the strict enum at response shaping "
        "rejects the whole batch: " + ", ".join(offenders)
    )


def test_non_member_classification_detector_bites():
    bad = ast.parse('def f():\n    return _failed_engine_result(classification="totally_made_up")\n')
    offenders = list(_non_member_classification_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"}))
    assert len(offenders) == 1 and "CrawlClassification member" in offenders[0], offenders
    good = ast.parse('def f(cls):\n    return _failed_engine_result(classification=cls.url_budget_exceeded)\n')
    assert not list(_non_member_classification_offenders(good, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"}))

    # The terminal-CrawlResult chokepoint is covered too: the render path builds
    # terminal results through it, so an unknown literal there reaches the wire
    # through the same lenient dataclass that let url_budget_exceeded ship.
    bad_terminal = ast.parse('def f():\n    return _terminal_crawl_result(classification="made_up_render_code")\n')
    offenders = list(_non_member_classification_offenders(bad_terminal, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"}))
    assert len(offenders) == 1 and "CrawlClassification member" in offenders[0], offenders
    good_terminal = ast.parse('def f(cls):\n    return _terminal_crawl_result(classification=cls.site_error)\n')
    assert not list(_non_member_classification_offenders(good_terminal, REPO_ROOT / "app" / "_synthetic.py", members={"rendered"}))


# ---------------------------------------------------------------------------
# Strategy-label validity chokepoint (the sibling of the value-validity one)
#
# The same class of bug hit the strategy axis: the browser pool worker labeled
# its terminal result ``strategy="BROWSER_DECLARED_CRAWLER"`` -- a string that
# is not a CrawlStrategy member -- while the scheduler's worker-exception
# envelope and the engine's budget-timeout envelope both passed ``strategy=""``.
# The internal CrawlStrategyName Literal and the strict CrawlStrategy enum on
# the wire model reject both values; the only reason anything worked at all was
# a pair of ``# type: ignore[arg-type]`` at the CrawlStrategyAttempt
# construction sites plus _shape_all's silent catch, which threw away the real
# classification and replaced it with site_error. A non-member strategy literal
# must never reach a result primitive again.
# ---------------------------------------------------------------------------

_STRATEGY_VALUE_SOURCES = ("strategy",)


def _non_member_strategy_offenders(tree, path, *, members):
    """A literal strategy passed to a result primitive must be a real member.

    ``members`` is the live ``CrawlStrategy`` value set. ``None`` is admitted
    explicitly: it is the honest label for "no strategy was attempted" (an
    envelope built after a worker raised, before any strategy ran), which is
    not an invented member and not an empty string. There is no
    hand-maintained exemption list -- such a list is precisely how the invented
    name shipped, because it would have to be remembered and updated in
    lockstep with the enum. A non-literal (variable, attribute, or module
    constant) is what callers should write and passes without comment.
    """
    for node, value in call_keyword_values(
        tree,
        function_names=_result_primitive_names(),
        keyword_names=_STRATEGY_VALUE_SOURCES,
    ):
        if isinstance(value, ast.Constant) and value.value is None:
            continue
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        if value.value not in members:
            yield offender(
                path,
                node.lineno,
                f"strategy={value.value!r} is not a CrawlStrategy member; "
                f"pass a real member, or None when no strategy was attempted",
            )


def test_engine_emitted_strategies_are_enum_members():
    from app.api_models import CrawlStrategy

    members = {m.value for m in CrawlStrategy}
    offenders = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_non_member_strategy_offenders(tree, path, members=members))
    assert not offenders, (
        "a literal strategy passed to a result primitive must be a "
        "CrawlStrategy member, or None when no strategy was attempted; the "
        "strict enum on the wire model rejects anything else and the shaping "
        "fallback silently overwrites the real classification: " + ", ".join(offenders)
    )


def test_non_member_strategy_detector_bites():
    from app.api_models import CrawlStrategy

    members = {m.value for m in CrawlStrategy}
    # An invented member name -- exactly the shape that shipped on the browser
    # worker's failure path.
    bad = ast.parse(
        'def f():\n    return _engine_result_from_exception(strategy="BROWSER_DECLARED_CRAWLER")\n'
    )
    offenders = list(
        _non_member_strategy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members)
    )
    assert len(offenders) == 1 and "CrawlStrategy member" in offenders[0], offenders
    # The empty string is the same lie in cheaper clothing.
    bad_empty = ast.parse('def f():\n    return _failed_engine_result(strategy="")\n')
    offenders = list(
        _non_member_strategy_offenders(bad_empty, REPO_ROOT / "app" / "_synthetic.py", members=members)
    )
    assert len(offenders) == 1 and "CrawlStrategy member" in offenders[0], offenders
    # None is admitted: it is the honest "no strategy attempted" label.
    good_none = ast.parse("def f():\n    return _failed_engine_result(strategy=None)\n")
    assert not list(
        _non_member_strategy_offenders(good_none, REPO_ROOT / "app" / "_synthetic.py", members=members)
    )
    # A real member literal and a non-literal both pass without comment.
    good_member = ast.parse('def f():\n    return _skipped_engine_result(strategy="BROWSER_HEADLESS")\n')
    assert not list(
        _non_member_strategy_offenders(good_member, REPO_ROOT / "app" / "_synthetic.py", members=members)
    )
    good_var = ast.parse("def f(s):\n    return _crawl_result_from_exception(strategy=s)\n")
    assert not list(
        _non_member_strategy_offenders(good_var, REPO_ROOT / "app" / "_synthetic.py", members=members)
    )


_HOST_POLICY_VALIDATORS = frozenset(
    {
        "canonical_page_identity",
        "filter_js_endpoints",
        "normalize_public_http_url",
        "public_url_or_none",
        "safe_public_url_or_none",
        "_validated_url",
    }
)
_HOST_POLICY_URL_CALLS = frozenset({"urljoin", "urlunsplit", "urlparse", "urlsplit", "geturl"})
_HOST_POLICY_URL_FIELDS = frozenset({"url", "raw", "href", "location", "normalized_url", "final_url"})
_HOST_POLICY_NAMES = frozenset({"policy", "host_policy"})


def _url_parameter_name(name: str) -> bool:
    lowered = name.lower()
    if lowered.endswith(("_of", "_getter", "_accessor")):
        return False
    return (
        "url" in lowered
        or lowered in {"href", "location", "origin", "endpoint", "endpoints"}
        or lowered.endswith("_href")
    )


def _host_policy_url_call(tree: ast.AST, node: ast.Call) -> bool:
    name = resolved_call_name(tree, node)
    return name.rsplit(".", 1)[-1].lower() in _HOST_POLICY_URL_CALLS or name.endswith(".SafeUrl.parse")


def _host_policy_validator_call(tree: ast.AST, node: ast.Call) -> bool:
    name = resolved_call_name(tree, node)
    return name.rsplit(".", 1)[-1] in _HOST_POLICY_VALIDATORS or name.endswith(".SafeUrl.parse")


def _host_policy_value(node: ast.AST | None, policy_names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in policy_names
    if isinstance(node, ast.Attribute) and node.attr in _HOST_POLICY_NAMES:
        return isinstance(node.value, ast.Name) and node.value.id == "self"
    return False


def _host_policy_scope_names(tree: ast.AST, function: ast.AST) -> set[str]:
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    names: set[str] = set()
    current: ast.AST | None = function
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = (*current.args.posonlyargs, *current.args.args, *current.args.kwonlyargs)
            names.update(argument.arg for argument in arguments if argument.arg in _HOST_POLICY_NAMES)
        current = parents.get(id(current))
    return names


def _host_policy_empty_guards(function: ast.AST) -> set[int]:
    guarded: set[int] = set()
    for node in _local_nodes(function):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        empty = (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Name)
        ) or (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and any(isinstance(op, (ast.Is, ast.Eq)) for op in test.ops)
            and any(isinstance(comparator, ast.Constant) and comparator.value in {None, ""} for comparator in test.comparators)
        )
        if empty:
            guarded.update(id(child) for child in node.body if isinstance(child, ast.Return))
    return guarded


def _host_policy_call_is_validated(tree: ast.AST, node: ast.Call, policy_names: set[str]) -> bool:
    if not _host_policy_validator_call(tree, node):
        return False
    return any(
        _host_policy_value(value, policy_names)
        for value in (*node.args, *(keyword.value for keyword in node.keywords))
    )


def _host_policy_expression_state(
    tree: ast.AST,
    node: ast.AST | None,
    url_names: set[str],
    validated_names: set[str],
    policy_names: set[str],
) -> tuple[bool, bool]:
    if node is None:
        return False, False
    if isinstance(node, ast.Name):
        return node.id in url_names, node.id in validated_names
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.lower().startswith(("http://", "https://")), False
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id in url_names
            and node.attr in _HOST_POLICY_URL_FIELDS
        ), False
    if isinstance(node, ast.Subscript):
        key = node.slice
        return (
            isinstance(node.value, ast.Name)
            and node.value.id in url_names
            and isinstance(key, ast.Constant)
            and key.value in _HOST_POLICY_URL_FIELDS
        ), False
    if isinstance(node, ast.Call):
        if _host_policy_call_is_validated(tree, node, policy_names):
            return True, True
        if _host_policy_validator_call(tree, node):
            return True, False
        if _host_policy_url_call(tree, node):
            return True, False
        if resolved_call_name(tree, node).rsplit(".", 1)[-1] == "get" and node.args:
            key = node.args[0]
            receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
            return (
                isinstance(receiver, ast.Name)
                and receiver.id in url_names
                and isinstance(key, ast.Constant)
                and key.value in _HOST_POLICY_URL_FIELDS
            ), False
        if resolved_call_name(tree, node).rsplit(".", 1)[-1] in {"str", "format", "join"}:
            child_states = [
                _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
                for child in (*node.args, *(keyword.value for keyword in node.keywords))
            ]
            return any(is_url for is_url, _ in child_states), any(
                is_validated for _, is_validated in child_states
            )
        return False, False
    if isinstance(node, ast.Dict):
        return any(
            isinstance(key, ast.Constant)
            and key.value in _HOST_POLICY_URL_FIELDS
            and _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)[0]
            for key, value in zip(node.keys, node.values, strict=True)
        ), any(
            isinstance(key, ast.Constant)
            and key.value in _HOST_POLICY_URL_FIELDS
            and _host_policy_expression_state(tree, value, url_names, validated_names, policy_names)[1]
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        states = [
            _host_policy_expression_state(tree, element, url_names, validated_names, policy_names)
            for element in node.elts
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
        state = _host_policy_expression_state(tree, node.elt, url_names, validated_names, policy_names)
        generator_url = any(_url_parameter_name(generator.iter.id) for generator in node.generators if isinstance(generator.iter, ast.Name))
        condition_validated = any(
            _host_policy_call_is_validated(tree, child, policy_names)
            for generator in node.generators
            for condition in generator.ifs
            for child in _local_nodes(condition)
            if isinstance(child, ast.Call)
        )
        return state[0] or generator_url, state[1] or condition_validated
    if isinstance(node, ast.DictComp):
        states = [
            _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
            for child in (node.key, node.value)
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, (ast.BinOp, ast.BoolOp, ast.JoinedStr, ast.IfExp, ast.Compare)):
        states = [
            _host_policy_expression_state(tree, child, url_names, validated_names, policy_names)
            for child in ast.iter_child_nodes(node)
        ]
        return any(is_url for is_url, _ in states), any(is_valid for _, is_valid in states)
    if isinstance(node, ast.NamedExpr):
        return _host_policy_expression_state(tree, node.value, url_names, validated_names, policy_names)
    return False, False


def _url_return_shapes(function: ast.AST, tree: ast.AST) -> bool:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    arguments = (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs)
    url_names = {argument.arg for argument in arguments if _url_parameter_name(argument.arg)}
    policy_names = _host_policy_scope_names(tree, function)
    validated_names: set[str] = set()
    nodes = list(_local_nodes(function))
    changed = True
    while changed:
        changed = False
        for loop in (node for node in nodes if isinstance(node, ast.For)):
            if not isinstance(loop.iter, ast.Name) or loop.iter.id not in url_names:
                continue
            target_names = {target.id for target in loop.target.elts if isinstance(target, ast.Name)} if isinstance(loop.target, (ast.Tuple, ast.List)) else ({loop.target.id} if isinstance(loop.target, ast.Name) else set())
            url_names.update(target_names)
            if any(
                _host_policy_call_is_validated(tree, child, policy_names)
                for child in _local_nodes(loop)
                if isinstance(child, ast.Call)
            ):
                validated = {loop.iter.id, *target_names}
                changed |= bool(validated - validated_names)
                validated_names.update(validated)
        for statement in nodes:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(statement, ast.Assign):
                targets = statement.targets
                value = statement.value
            elif isinstance(statement, ast.AnnAssign):
                targets = [statement.target]
                value = statement.value
            elif isinstance(statement, ast.NamedExpr):
                targets = [statement.target]
                value = statement.value
            if value is None:
                continue
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            if not names:
                continue
            is_url, is_validated = _host_policy_expression_state(
                tree, value, url_names, validated_names, policy_names
            )
            if is_url:
                changed |= bool(names - url_names)
                url_names.update(names)
            if is_validated:
                changed |= bool(names - validated_names)
                validated_names.update(names)
            if _host_policy_value(value, policy_names):
                changed |= bool(names - policy_names)
                policy_names.update(names)
    endpoint_parameters = {
        argument.arg for argument in arguments if argument.arg.lower() in {"endpoint", "endpoints"}
    }
    empty_guards = _host_policy_empty_guards(function)
    for statement in nodes:
        if not isinstance(statement, ast.Return) or id(statement) in empty_guards:
            continue
        is_url, is_validated = _host_policy_expression_state(
            tree, statement.value, url_names, validated_names, policy_names
        )
        if is_url and not is_validated:
            return True
        if isinstance(statement.value, ast.Name) and statement.value.id in endpoint_parameters:
            return True
    return False


def _required_host_policy_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _url_return_shapes(node, tree):
            found.append(offender(path, node.lineno, f"{node.name}() returns URLs without required HostPolicy"))
    return found


def test_required_host_policy_detector_bites():
    bad = ast.parse(
        "def normalize_public_http_url(url, *, policy=None):\n"
        "    return url\n"
    )
    offenders = _required_host_policy_offenders(
        bad,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert len(offenders) == 1 and "HostPolicy" in offenders[0], offenders

    good = ast.parse(
        "from app.validation import public_url_or_none\n"
        "def normalize_public_http_url(url, *, policy: HostPolicy):\n"
        "    return public_url_or_none(url, policy=policy)\n"
    )
    assert not _required_host_policy_offenders(
        good,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    safe_bad = ast.parse(
        "class SafeUrl:\n"
        "    def parse(self, url, *, policy=None):\n"
        "        return url\n"
    )
    assert len(
        _required_host_policy_offenders(
            safe_bad,
            REPO_ROOT / "app" / "_synthetic.py",
        )
    ) == 1


_HOST_POLICY_PROPERTY = Property(
    name="host-policy",
    owner_module="app/validation.py",
    owner_symbols=("_host_policy_reason",),
    definition_site="app/validation.py:_host_policy_reason",
    fingerprints=("host_policy_validation",),
    reason="Every URL boundary must apply the host policy before forwarding a URL.",
)


def test_host_policy_detector_property_bites():
    assert_property_detector_bites(
        _HOST_POLICY_PROPERTY,
        lambda tree, path, _prop: _required_host_policy_offenders(tree, path),
        caught={
            "dotted urljoin import": (
                "from urllib.parse import urljoin\n"
                "def f(root, path):\n"
                "    return urljoin(root, path)\n",
                1,
            ),
            "annotation without policy use": (
                "def f(url, *, policy: HostPolicy):\n"
                "    return url\n",
                1,
            ),
            "callable indirection without policy": (
                "from app.validation import public_url_or_none\n"
                "def f(url, *, policy: HostPolicy):\n"
                "    validator = public_url_or_none\n"
                "    return validator(url)\n",
                1,
            ),
            "dotted urlunsplit import": (
                "from urllib.parse import urlunsplit\n"
                "def f(parts):\n"
                "    return urlunsplit(parts)\n",
                1,
            ),
        },
        quiet={
            "dotted urljoin validated": (
                "from urllib.parse import urljoin\n"
                "from app.validation import public_url_or_none\n"
                "def f(root, path, *, policy: HostPolicy):\n"
                "    return public_url_or_none(urljoin(root, path), policy=policy)\n"
            ),
            "annotation replaced by policy use": (
                "class Client:\n"
                "    policy: HostPolicy\n"
                "    def emit(self, url):\n"
                "        return public_url_or_none(url, policy=self.policy)\n"
            ),
            "callable indirection with policy": (
                "from app.validation import public_url_or_none as validate\n"
                "def f(url, *, policy: HostPolicy):\n"
                "    return validate(url, policy=policy)\n"
            ),
            "dotted urlunsplit validated": (
                "from urllib.parse import urlunsplit\n"
                "from app.validation import public_url_or_none\n"
                "def f(parts, *, policy: HostPolicy):\n"
                "    return public_url_or_none(urlunsplit(parts), policy=policy)\n"
            ),
        },
    )


def test_required_host_policy_detector_covers_new_url_helper_shapes():
    source = ast.parse(
        "def passthrough_url(url: str) -> str:\n"
        "    return url\n"
        "def filter_endpoints(endpoints: list[dict[str, str]]) -> list[dict[str, str]]:\n"
        "    return [item for item in endpoints if item.get('url')]\n"
    )
    offenders = _required_host_policy_offenders(
        source,
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert len(offenders) == 2, offenders


def test_host_policy_detector_accepts_fixed_extraction_boundaries():
    path = REPO_ROOT / "app" / "crawl" / "extraction.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not [
        offender
        for offender in _required_host_policy_offenders(tree, path)
        if any(name in offender for name in ("_extract_frames", "_extract_forms"))
    ]


def test_url_returning_helpers_require_host_policy():
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_required_host_policy_offenders(tree, path))
    assert not offenders, ", ".join(offenders)


def test_resolved_call_name_handles_getattr_alias_and_static_name():
    source = ast.parse(
        "from builtins import getattr as ga\n"
        "import ipaddress\n"
        "def direct():\n"
        "    return getattr(ipaddress, 'ip_address')\n"
        "def aliased(value):\n"
        "    return ga(ipaddress, 'ip_address')(value)\n"
        "def constructed(value):\n"
        "    name = 'ip_' + 'address'\n"
        "    return getattr(ipaddress, name)(value)\n"
    )
    calls = [node for node in ast.walk(source) if isinstance(node, ast.Call)]
    attribute_calls = [call for call in calls if isinstance(call.func, ast.Name)]
    assert len(attribute_calls) == 3
    assert all(resolved_call_name(source, call) == "ipaddress.ip_address" for call in attribute_calls)


# ---------------------------------------------------------------------------
# Normalize-or-None chokepoint (jscpd #5 family)
#
# The same three-line wrapper -- try normalize_public_http_url(x) except
# ValueError: return None -- was hand-rolled in three modules (search
# providers, map, batch dedupe). Each is a URL egress door that must apply
# the SSRF private-target policy; a new call site that skips the wrapper
# silently drops the policy for its door.
# ---------------------------------------------------------------------------

_NORMALIZE_OR_NONE_OFFENDERS = frozenset(
    {"normalize_public_http_url", "public_url_or_none", "safe_public_url_or_none"}
)


def _normalize_or_none_offenders(tree, path):
    """A try/except ValueError around the normalizer outside the chokepoint."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        body_names = {
            getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
        }
        if "normalize_public_http_url" not in body_names:
            continue
        for handler in node.handlers:
            if not (isinstance(handler.type, ast.Name) and handler.type.id == "ValueError"):
                continue
            # The re-inlined wrapper discards the exception: its body is a bare
            # None, pass, or a raw fallback (``x`` returned unchanged). A site
            # that re-raises, branches on the error text, or needs the
            # normalized value for a second computation (registrable_domain,
            # the error message) is a different contract and stays.
            body = handler.body
            if len(body) != 1:
                continue
            stmt = body[0]
            is_discard = (
                (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant) and stmt.value.value is None)
                or isinstance(stmt, ast.Pass)
                or (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and isinstance(stmt.value, ast.Name)
                    and stmt.value.id != stmt.targets[0].id)
            )
            if is_discard:
                yield offender(
                    path, node.lineno,
                    "normalize_public_http_url + except ValueError re-inlined; "
                    "use app.validation.safe_public_url_or_none",
                )


def test_normalize_or_none_is_not_reinlined():
    offenders = []
    # The chokepoint's own definition is the sanctioned site.
    for path, tree in app_trees(exclude=frozenset({"validation.py"})):
        offenders.extend(_normalize_or_none_offenders(tree, path))
    assert not offenders, ", ".join(offenders)


def test_normalize_or_none_detector_bites():
    bad = ast.parse(
        "def f(u):\n"
        "    try:\n"
        "        return normalize_public_http_url(u)\n"
        "    except ValueError:\n"
        "        return None\n"
    )
    offenders = list(_normalize_or_none_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "safe_public_url_or_none" in offenders[0], offenders
    good = ast.parse("def f(u):\n    return safe_public_url_or_none(u)\n")
    assert not list(_normalize_or_none_offenders(good, REPO_ROOT / "app" / "_synthetic.py"))


# ---------------------------------------------------------------------------
# Unfiltered urljoin egress doors (H class)
#
# _detect_pagination read an href out of attacker-controlled HTML and returned
# urljoin(final_url, href) raw. The HTTP consumer filtered it before use, the
# browser consumer did not, so a javascript: rel=next reached the response.
# Filtering moved to the producer; this keeps the next door from reopening.
# ---------------------------------------------------------------------------

_EGRESS_FILTERS = frozenset({"public_url_or_none", "safe_public_url_or_none"})


def _is_urljoin_call(node: ast.AST) -> bool:
    """True for a plain ``urljoin(...)`` call."""
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "urljoin"


def _unfiltered_urljoin_return_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a function that returns a ``urljoin`` result with no egress filter.

    Caught shapes: ``return urljoin(a, b)`` and ``u = urljoin(a, b); return u``.
    A URL resolved out of untrusted text is an egress door as soon as a caller
    trusts it, so the join and the filter belong together; the browser path
    proved that "every consumer remembers to filter" does not scale.
    ``app/safe_url.py`` composes ``urljoin`` inside the sanctioned
    ``SafeUrl.parse``/``redirect_hop`` path, so it is exempt by file, matching
    the other chokepoint-module exemptions.
    """
    if path.name == "safe_url.py":
        return []
    offenders: list[str] = []
    for func in _functions_with(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        joined: set[str] = set()
        returns_raw = False
        returns_binding = False
        for node in ast.walk(func):
            if isinstance(node, ast.Assign) and _is_urljoin_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        joined.add(target.id)
            elif isinstance(node, ast.Return):
                if _is_urljoin_call(node.value):
                    returns_raw = True
                elif isinstance(node.value, ast.Name) and node.value.id in joined:
                    returns_binding = True
        if not (returns_raw or returns_binding):
            continue
        uses_filter = any(
            (isinstance(n, ast.Name) and n.id in _EGRESS_FILTERS)
            or (isinstance(n, ast.Attribute) and n.attr in _EGRESS_FILTERS)
            for n in ast.walk(func)
        )
        if not uses_filter:
            offenders.append(
                offender(path, func.lineno, f"{func.name}() returns a urljoin with no egress filter")
            )
    return offenders


def test_no_unfiltered_urljoin_returns():
    """A URL joined from untrusted input is egress-filtered before it leaves."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_unfiltered_urljoin_return_offenders(tree, path))
    assert not offenders, "filter joined URLs through public_url_or_none: " + ", ".join(offenders)


def test_unfiltered_urljoin_detector_bites():
    """The historical leak shape fires; the filtered and sanctioned shapes stay clean."""
    raw = ast.parse("def _detect_pagination(html, final_url):\n    return urljoin(final_url, href)\n")
    offenders = _unfiltered_urljoin_return_offenders(raw, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "no egress filter" in offenders[0], offenders
    binding = ast.parse("def f(base, href):\n    u = urljoin(base, href)\n    return u\n")
    assert _unfiltered_urljoin_return_offenders(binding, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    good = ast.parse(
        "def f(href, final_url):\n    return public_url_or_none(href, resolve_against=final_url)\n"
    )
    assert not _unfiltered_urljoin_return_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # safe_url.py composes urljoin inside SafeUrl.parse / redirect_hop.
    hop = ast.parse("def hop(self, location):\n    return SafeUrl.parse(urljoin(self.raw, location), pin=True)\n")
    assert not _unfiltered_urljoin_return_offenders(hop, REPO_ROOT / "app" / "safe_url.py")


# ---------------------------------------------------------------------------
# Raw exception text never reaches the wire (information disclosure)
#
# render.py built the worker-failure result with ``error=str(error)`` from a
# caught exception, and ``to_dict`` is pure serialization -- so the raw text
# (DNS names, filesystem paths, secrets) reached the /v1/render body verbatim.
# ``_terminal_crawl_result`` is the sanctioned construction site and takes an
# already-classified error; this keeps raw text from drifting back into any
# error= field of a wire-visible result.
# ---------------------------------------------------------------------------

_TERMINAL_RESULT_SANCTIONED = frozenset({"result_helpers.py"})
_RESULT_ERROR_SINKS = frozenset({"CrawlResult", "_terminal_crawl_result"})
_EXCEPTION_NAMES = frozenset({"error", "exc", "exception", "err"})


def _is_raw_exception_text(value: ast.expr) -> bool:
    """True for ``str(exc)``/``repr(exc)`` or an f-string interpolating one."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("str", "repr"):
        return True
    if isinstance(value, ast.JoinedStr):
        return any(
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id in _EXCEPTION_NAMES
            for part in value.values
        )
    return False


def _raw_exception_error_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a wire-visible result built with the raw text of an exception."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _RESULT_ERROR_SINKS
        ):
            continue
        for kw in node.keywords:
            if kw.arg == "error" and _is_raw_exception_text(kw.value):
                offenders.append(offender(path, node.lineno, f"{node.func.id}(error=<raw exception text>)"))
    return offenders


def test_no_raw_exception_text_in_result_error_field():
    """The ``error`` field is a stable classification, never ``str(exc)``."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=_TERMINAL_RESULT_SANCTIONED):
        offenders.extend(_raw_exception_error_offenders(tree, path))
    assert not offenders, "classify via _classify_error_code, never raw exception text: " + ", ".join(offenders)


def test_raw_exception_error_detector_bites():
    """The leak shape fires on both sinks; the classified shape stays clean."""
    direct = ast.parse("def f(exc):\n    return CrawlResult(error=str(exc))\n")
    offenders = _raw_exception_error_offenders(direct, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 1 and "raw exception text" in offenders[0], offenders
    via_primitive = ast.parse("def f(exc):\n    return _terminal_crawl_result(error=str(exc))\n")
    assert _raw_exception_error_offenders(via_primitive, REPO_ROOT / "app" / "api" / "_synthetic.py")
    fstring = ast.parse("def f(error):\n    return CrawlResult(error=f'failed: {error}')\n")
    assert _raw_exception_error_offenders(fstring, REPO_ROOT / "app" / "api" / "_synthetic.py")
    repr_shape = ast.parse("def f(e):\n    return CrawlResult(error=repr(e))\n")
    assert _raw_exception_error_offenders(repr_shape, REPO_ROOT / "app" / "api" / "_synthetic.py")
    good = ast.parse("def f(exc):\n    return _terminal_crawl_result(error=_classify_error_code(exc))\n")
    assert not _raw_exception_error_offenders(good, REPO_ROOT / "app" / "api" / "_synthetic.py")


# ---------------------------------------------------------------------------
# JS-endpoint egress door: the filter must not be reinlined
#
# ``filter_js_endpoints`` is the single place a scraped JS endpoint survives or
# is dropped. Both call sites used to carry the inline comprehension
# (``{**endpoint, "url": clean} for ... if (clean := public_url_or_none(...))
# is not None``) -- the shape that silently disappears the moment a consumer is
# added in a hurry. This pins the comprehension to the chokepoint itself.
# ---------------------------------------------------------------------------

_JS_ENDPOINTS_FILTER_SANCTIONED = frozenset({"validation.py"})


def _reinline_js_endpoints_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag the inlined endpoint-filter comprehension outside its chokepoint.

    The reinlined shape is a comprehension whose element spreads an existing
    dict and rewrites its ``url`` key (``{**endpoint, "url": clean}``) -- a
    None key in the dict is the ``**`` spread, and the rewritten ``url`` key is
    what makes it an endpoint filter rather than an ordinary copy.
    """
    if path.name == "validation.py":
        return []
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            continue
        elt = node.elt
        if not isinstance(elt, ast.Dict):
            continue
        if any(k is None for k in elt.keys) and any(
            isinstance(k, ast.Constant) and k.value == "url" for k in elt.keys
        ):
            offenders.append(
                offender(path, node.lineno, "inlined js_endpoints filter: use validation.filter_js_endpoints")
            )
    return offenders


def test_js_endpoints_filter_is_not_reinlined():
    """Scraped JS endpoints are filtered through ``filter_js_endpoints``."""
    offenders: list[str] = []
    for path, tree in app_trees(exclude=_JS_ENDPOINTS_FILTER_SANCTIONED):
        offenders.extend(_reinline_js_endpoints_filter_offenders(tree, path))
    assert not offenders, "route endpoints through filter_js_endpoints: " + ", ".join(offenders)


def test_reinline_js_endpoints_detector_bites():
    """The historical inline shape fires; the chokepoint call stays clean."""
    bad = ast.parse(
        "def f(endpoints, base):\n"
        '    return [{**e, "url": c} for e in endpoints\n'
        '           if (c := public_url_or_none(e["url"])) is not None]\n'
    )
    offenders = _reinline_js_endpoints_filter_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(offenders) == 1 and "filter_js_endpoints" in offenders[0], offenders
    good = ast.parse("def f(endpoints, base):\n    return filter_js_endpoints(endpoints, resolve_against=base)\n")
    assert not _reinline_js_endpoints_filter_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # The chokepoint's own comprehension really is the matched shape: the file
    # exemption is what keeps the real tree clean, not a predicate that misses it.
    validation = next(((path, tree) for path, tree in app_trees() if path.name == "validation.py"), None)
    assert validation is not None
    validation_path, validation_tree = validation
    assert _reinline_js_endpoints_filter_offenders(validation_tree, validation_path) == []
    hits = _reinline_js_endpoints_filter_offenders(validation_tree, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    assert len(hits) == 1 and "filter_js_endpoints" in hits[0], hits


# ---------------------------------------------------------------------------
# Chokepoint drift detectors for the P0-P2 fix tiers
#
# Each behavioral test below pins what the code DOES today; these pin the
# CHOKEPOINT so a future edit that bypasses it turns the suite red instead of
# silently reintroducing the defect. Same shape as the detectors above: an AST
# walk over app/, a synthetic *_bites companion proving the predicate fires,
# and no hand-maintained exemption list.
# ---------------------------------------------------------------------------


def _enclosing_defs(node: ast.AST, root: ast.AST) -> list[str]:
    """The enclosing ``def``/``class`` names from innermost outward.

    AST has no parent pointers, so a parent map is built per tree; the walk is
    O(nodes) and the detectors call it only on a handful of matched nodes.
    """
    parents: dict[int, ast.AST] = {}
    stack = [root]
    while stack:
        parent = stack.pop()
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
            stack.append(child)
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(id(current))
    return names


# --- 4a: the robots matcher compiler is reachable only via its host cache ----

_ROBOTS_COMPILE = "_compile_robots_pattern"
_ROBOTS_COMPILE_HOME = "_RobotsPatternCache"


def _robots_compile_call_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """``_compile_robots_pattern`` may be called only from ``_RobotsPatternCache.get``.

    The pure pattern translation is the cheap half of the robots matcher; the
    safety property is that every caller reaches it through the bounded
    PER-HOST LRU. A second caller reintroduces a process-global matcher cache
    keyed only on the pattern string -- patterns arrive from remotely-fetched
    robots.txt, so a hostile site spraying distinct patterns would evict every
    other site's matchers and control the whole cache (P2-E). The home method
    is the single sanctioned call site; the module-level wrapper
    (``_robots_pattern_to_regex``) goes through it too.
    """
    offenders: list[str] = []
    for node in calls_named(tree, _ROBOTS_COMPILE):
        scopes = _enclosing_defs(node, tree)
        if _ROBOTS_COMPILE_HOME in scopes and "get" in scopes[: scopes.index(_ROBOTS_COMPILE_HOME) + 1]:
            continue
        offenders.append(
            offender(
                path,
                node.lineno,
                f"{_ROBOTS_COMPILE} called outside {_ROBOTS_COMPILE_HOME}.get; route through the"
                " per-host cache so a hostile robots.txt cannot evict another site's matchers",
            )
        )
    return offenders


def test_robots_pattern_compile_lives_behind_the_per_host_cache():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_robots_compile_call_offenders(tree, path))
    assert not offenders, ", ".join(offenders)


def test_robots_pattern_compile_detector_bites():
    # A direct caller -- exactly the shape that reintroduces a global,
    # attacker-keyed matcher cache.
    bad = ast.parse("def f(pattern):\n    return _compile_robots_pattern(pattern)\n")
    offenders = list(_robots_compile_call_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "per-host cache" in offenders[0], offenders
    # The sanctioned home passes.
    good = ast.parse(
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        return _compile_robots_pattern(pattern)\n"
    )
    assert not _robots_compile_call_offenders(good, REPO_ROOT / "app" / "_synthetic.py")
    # A nested helper inside the home class is still inside the home scope.
    nested = ast.parse(
        "class _RobotsPatternCache:\n"
        "    def get(self, host, pattern):\n"
        "        def build():\n"
        "            return _compile_robots_pattern(pattern)\n"
        "        return build()\n"
    )
    assert not _robots_compile_call_offenders(nested, REPO_ROOT / "app" / "_synthetic.py")
    # A same-named method on any OTHER class is not the home.
    impostor = ast.parse(
        "class OtherCache:\n"
        "    def get(self, pattern):\n"
        "        return _compile_robots_pattern(pattern)\n"
    )
    assert len(_robots_compile_call_offenders(impostor, REPO_ROOT / "app" / "_synthetic.py")) == 1


# --- 4b: result-primitive status values are real ResultStatus members --------

_STATUS_VALUE_SOURCES = ("status",)


def _non_member_status_offenders(tree: ast.AST, path: pathlib.Path, *, members) -> list[str]:
    """A literal ``status`` passed to a result primitive must be a real member.

    Mirrors the classification/strategy detectors: ``status`` feeds the strict
    wire enum on the unified result, so an invented literal is a KeyError or a
    silent shaping failure waiting on a batch. ``members`` is the live
    ``ResultStatus`` argument set, read at test time -- a hand-maintained
    exemption list is precisely how an invented status would ship.
    """
    offenders: list[str] = []
    for node, value in call_keyword_values(
        tree,
        function_names=_result_primitive_names(),
        keyword_names=_STATUS_VALUE_SOURCES,
    ):
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if value.value not in members:
            offenders.append(
                offender(
                    path,
                    node.lineno,
                    f"status={value.value!r} is not a ResultStatus member; add it to the"
                    " Literal (app/models.py) or derive it from an existing member",
                )
            )
    return offenders


def test_result_primitives_emit_real_status_members():
    from typing import get_args

    from app.models import ResultStatus

    members = set(get_args(ResultStatus))
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_non_member_status_offenders(tree, path, members=members))
    assert not offenders, (
        "a literal status passed to a result primitive must be a ResultStatus "
        "member, or the strict enum on the wire model rejects the batch: " + ", ".join(offenders)
    )


def test_non_member_status_detector_bites():
    from typing import get_args

    from app.models import ResultStatus

    members = set(get_args(ResultStatus))
    bad = ast.parse('def f():\n    return _terminal_crawl_result(status="never_a_status")\n')
    offenders = list(_non_member_status_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members))
    assert len(offenders) == 1 and "ResultStatus member" in offenders[0], offenders
    good = ast.parse('def f():\n    return _terminal_crawl_result(status="failed")\n')
    assert not _non_member_status_offenders(good, REPO_ROOT / "app" / "_synthetic.py", members=members)
    # A non-literal (a variable or the caller's own status field) passes.
    good_var = ast.parse("def f(s):\n    return _make_engine_result(status=s)\n")
    assert not _non_member_status_offenders(good_var, REPO_ROOT / "app" / "_synthetic.py", members=members)


# --- 4c: the webhook semaphore is touched only through its loop accessor -----

_SEMAPHORE_GLOBALS = frozenset({"_async_crawl_semaphore", "_async_crawl_semaphore_loop"})
_SEMAPHORE_ACCESSOR = "_get_async_crawl_semaphore"


def _semaphore_global_touch_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """The loop-bound semaphore module globals are touched only by the accessor.

    ``_get_async_crawl_semaphore`` is the single home that binds the cached
    instance to the running loop and rebuilds it when the loop changes; reading
    or assigning the module global from anywhere else silently reintroduces a
    loop-bound violation (a Semaphore awaited off the loop it was created on).
    The module-level declaration has no enclosing function and is exempt.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in _SEMAPHORE_GLOBALS):
            continue
        scopes = _enclosing_defs(node, tree)
        if not scopes or _SEMAPHORE_ACCESSOR in scopes:
            continue
        offenders.append(
            offender(
                path,
                node.lineno,
                f"{node.id} touched outside {_SEMAPHORE_ACCESSOR}; use the accessor so the"
                " instance stays bound to the running loop",
            )
        )
    return offenders


def test_async_crawl_semaphore_globals_are_loop_bound():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_semaphore_global_touch_offenders(tree, path))
    assert not offenders, ", ".join(offenders)


def test_async_crawl_semaphore_loop_binding_detector_bites():
    # A direct read from a random function -- the loop-bound bypass.
    bad = ast.parse("async def f():\n    return _async_crawl_semaphore.acquire()\n")
    offenders = list(_semaphore_global_touch_offenders(bad, REPO_ROOT / "app" / "_synthetic.py"))
    assert len(offenders) == 1 and "loop" in offenders[0], offenders
    # A direct write is the same bypass.
    bad_write = ast.parse("def f():\n    _async_crawl_semaphore = None\n")
    assert list(_semaphore_global_touch_offenders(bad_write, REPO_ROOT / "app" / "_synthetic.py"))
    # The module-level declaration is the definition, not a touch.
    declaration = ast.parse("_async_crawl_semaphore: asyncio.Semaphore | None = None\n")
    assert not _semaphore_global_touch_offenders(declaration, REPO_ROOT / "app" / "_synthetic.py")
    # The accessor itself is exempt, including the companion loop global.
    accessor = ast.parse(
        "def _get_async_crawl_semaphore():\n"
        "    global _async_crawl_semaphore, _async_crawl_semaphore_loop\n"
        "    if _async_crawl_semaphore is None:\n"
        "        _async_crawl_semaphore = _build_async_crawl_semaphore()\n"
        "    return _async_crawl_semaphore\n"
    )
    assert not _semaphore_global_touch_offenders(accessor, REPO_ROOT / "app" / "_synthetic.py")


# --- 4d: the doc pool reaps retired slots on every acquisition ---------------

_DOC_POOL_NAME = "_DOC_POOL"
_DOC_POOL_ACQUIRER = "_acquire_doc_worker"


def _has_retired_attr(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Attribute) and sub.attr == "retired" for sub in ast.walk(node))


def _reaps_retired(value: ast.AST) -> bool:
    """A ``_DOC_POOL = [... for each ... if not each.retired]`` style rebuild.

    ``not each.retired`` parses as a ``UnaryOp(Not, ...)`` and an explicit
    comparison as a ``Compare``; both shapes are admitted.
    """
    if not isinstance(value, ast.ListComp):
        return False
    for generator in value.generators:
        for condition in generator.ifs:
            for node in ast.walk(condition):
                if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                    if _has_retired_attr(node.operand):
                        return True
                if isinstance(node, ast.Compare):
                    if any(_has_retired_attr(operand) for operand in (node.left, *node.comparators)):
                        return True
    return False


def _doc_pool_reap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """``_acquire_doc_worker`` must rebuild the pool without retired slots.

    P2-D: a retired slot has been terminated and can never be handed out, so it
    must not keep occupying a pool entry. The respawn scan only replaces a slot
    whose process is dead, which leaves a retired-but-still-``is_alive()`` slot
    skipped by both scans and stranded in the pool forever -- the entry count
    drifted permanently above the usable count. Removing the reap filter is the
    regression; this detector fails the suite when it goes.
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == _DOC_POOL_ACQUIRER):
            continue
        if not any(
            isinstance(child, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == _DOC_POOL_NAME for target in child.targets)
            and _reaps_retired(child.value)
            for child in ast.walk(node)
        ):
            offenders.append(
                offender(
                    path,
                    node.lineno,
                    f"{_DOC_POOL_ACQUIRER} no longer reaps retired slots; a retired-but-alive"
                    " slot would strand in the pool and inflate the size budget",
                )
            )
    return offenders


def test_doc_pool_acquirer_reaps_retired_slots():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_doc_pool_reap_offenders(tree, path))
    assert not offenders, ", ".join(offenders)


def test_doc_pool_reap_detector_bites():
    # The acquirer without the reap filter: the regression the detector hunts.
    bad = ast.parse(
        "def _acquire_doc_worker():\n"
        "    with _POOL_LOCK:\n"
        "        want = _doc_extraction_worker_count()\n"
        "        while True:\n"
        "            for worker in _DOC_POOL:\n"
        "                if not worker.busy:\n"
        "                    return worker\n"
    )
    offenders = list(_doc_pool_reap_offenders(bad, REPO_ROOT / "app" / "crawl" / "_synthetic.py"))
    assert len(offenders) == 1 and "retired" in offenders[0], offenders
    # The real filter satisfies it.
    good = ast.parse(
        "def _acquire_doc_worker():\n"
        "    with _POOL_LOCK:\n"
        "        _DOC_POOL = [each for each in _DOC_POOL if not each.retired]\n"
        "        return _DOC_POOL[0]\n"
    )
    assert not _doc_pool_reap_offenders(good, REPO_ROOT / "app" / "crawl" / "_synthetic.py")
    # A pool rebuild that filters on something else is not the reap.
    not_reap = ast.parse(
        "def _acquire_doc_worker():\n"
        "    with _POOL_LOCK:\n"
        "        _DOC_POOL = [each for each in _DOC_POOL if not each.busy]\n"
        "        return _DOC_POOL[0]\n"
    )
    assert list(_doc_pool_reap_offenders(not_reap, REPO_ROOT / "app" / "crawl" / "_synthetic.py"))


# --- 4e (optional): the attempt dataclass is pinned at its construction site ---
#
# ``CrawlStrategyName`` is now a true Literal (the strategy constants in
# ``app/constants.py`` are ``Final``, so ``Literal["HTTP_DECLARED_CRAWLER", ...]``
# resolves and mypy enforces the ``strategy=`` arg-type at every
# ``CrawlStrategyAttempt`` construction site statically. That replaced the six
# ``# type: ignore[arg-type]`` comments the producers used to carry. This
# detector is now belt-and-suspenders with mypy: it stays because it runs at
# pytest time and flags a literal that slips past the checker, but the static
# gate is the real chokepoint now.

_ATTEMPT_CLASS = "CrawlStrategyAttempt"
_ATTEMPT_STRATEGY_KWARG = "strategy"


def _non_member_attempt_strategy_offenders(tree: ast.AST, path: pathlib.Path, *, members) -> list[str]:
    """A literal ``strategy=`` at a ``CrawlStrategyAttempt`` site must be a member.

    Covers the class directly rather than the builder primitives, so a new
    construction site anywhere in ``app/`` is policed without being registered
    anywhere first. ``None`` is admitted: it is the honest label for "no
    strategy was attempted". ``members`` is the live ``CrawlStrategyName``
    argument set, read at test time.
    """
    offenders: list[str] = []
    for node, value in call_keyword_values(
        tree,
        function_names=_ATTEMPT_CLASS,
        keyword_names=_ATTEMPT_STRATEGY_KWARG,
    ):
        if isinstance(value, ast.Constant) and value.value is None:
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if value.value not in members:
            offenders.append(
                offender(
                    path,
                    node.lineno,
                    f"strategy={value.value!r} is not a CrawlStrategyName member; pass a real"
                    " member, or None when no strategy was attempted",
                )
            )
    return offenders


def test_crawl_strategy_attempt_sites_use_real_members():
    from typing import get_args

    from app.models import CrawlStrategyName

    members = set(get_args(CrawlStrategyName))
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_non_member_attempt_strategy_offenders(tree, path, members=members))
    assert not offenders, (
        "a literal strategy at a CrawlStrategyAttempt construction site must be a "
        "CrawlStrategyName member, or None when no strategy was attempted: " + ", ".join(offenders)
    )


def test_crawl_strategy_attempt_detector_bites():
    from typing import get_args

    from app.models import CrawlStrategyName

    members = set(get_args(CrawlStrategyName))
    bad = ast.parse(
        "def f():\n    return CrawlStrategyAttempt(strategy='BROWSER_DECLARED_CRAWLER', status='failed',"
        " classification='site_error', http_status=None, content_type=None, elapsed_ms=0)\n"
    )
    offenders = list(_non_member_attempt_strategy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members))
    assert len(offenders) == 1 and "CrawlStrategyName member" in offenders[0], offenders
    # None is admitted -- the honest "no strategy attempted" label.
    good_none = ast.parse(
        "def f():\n    return CrawlStrategyAttempt(strategy=None, status='failed',"
        " classification='site_error', http_status=None, content_type=None, elapsed_ms=0)\n"
    )
    assert not _non_member_attempt_strategy_offenders(good_none, REPO_ROOT / "app" / "_synthetic.py", members=members)
    # A real member literal and a non-literal both pass.
    good_member = ast.parse("def f():\n    return CrawlStrategyAttempt(strategy='BROWSER_HEADLESS')\n")
    assert not _non_member_attempt_strategy_offenders(good_member, REPO_ROOT / "app" / "_synthetic.py", members=members)
    good_var = ast.parse("def f(s):\n    return CrawlStrategyAttempt(strategy=s)\n")
    assert not _non_member_attempt_strategy_offenders(good_var, REPO_ROOT / "app" / "_synthetic.py", members=members)
def test_crawl_strategy_name_members_match_constants():
    """The ``CrawlStrategyName`` Literal must not drift from ``app.constants``.

    ``CrawlStrategyName`` cannot reference the ``Final`` constants directly inside
    ``Literal[...]`` (mypy rejects that), so its members are spelled as string
    literals. constants.py remains the single source of truth; this pins the two
    together so a rename there cannot orphan the alias.
    """
    from typing import get_args

    from app import constants
    from app.models import CrawlStrategyName

    declared = {
        constants.HTTP_DECLARED_CRAWLER,
        constants.HTTP_CFFI_IMPERSONATE,
        constants.BROWSER_HEADLESS,
        constants.BROWSER_HEADED_WARMED,
    }
    assert set(get_args(CrawlStrategyName)) == declared


def test_crawl_error_members_derive_from_classification():
    """Every Classification member is a ``CrawlError`` member, and vice versa
    only for the documented pipeline additions.

    ``CrawlError`` cannot build itself from ``Classification`` with
    ``*get_args()`` inside ``Literal[...]`` (mypy cannot evaluate that), so its
    members are spelled as string literals. models.py remains the single source
    of truth for the page-outcome half; this pins the two together so a
    Classification member added upstream cannot be forgotten here -- the exact
    drift the derivation was written to prevent.
    """
    from typing import get_args

    from app.models import Classification, CrawlError

    error_members = set(get_args(CrawlError))
    classification_members = set(get_args(Classification))
    assert classification_members <= error_members, (
        "every Classification member must be a CrawlError member (the builders"
        f" route error=classification): missing {sorted(classification_members - error_members)}"
    )
    # The only codes beyond Classification are the documented pipeline
    # conditions with no page-classification analogue. A stray addition here is
    # a code callers cannot rely on, and a removal silently narrows the field.
    extras = error_members - classification_members
    assert extras == {
        "robots_txt_disallowed",
        "browser_worker_unavailable",
        "unsupported_strategy",
        "retries_exhausted",
    }, sorted(extras)


# ===========================================================================
# GENERIC value-membership chokepoint (program 1)
#
# The detectors above were written one per escaped field: classification after
# url_budget_exceeded, strategy after BROWSER_DECLARED_CRAWLER, status after
# the terminal-result widening. Each is a list nobody extends, so the class
# re-emerged at every field not on the list. The replacement is one generic
# sweep: the (dataclass, field, Literal) triples are read from app.models at
# test time, so a field added to a result dataclass tomorrow is covered the day
# it lands rather than the day someone remembers to name it.
#
# The old per-field detectors are kept below as *proof* the generic one is
# strictly stronger -- each is subsumed by the generic sweep, which is what
# makes deleting the per-field list safe. They are asserts on the generic
# detector's coverage, not on a hand-kept field list.
# ===========================================================================


def test_result_primitives_emit_only_declared_literal_members():
    """Every literal value handed to a result primitive is a member of its
    field's declared ``Literal`` -- the generic, field-axis-closed form of the
    per-field detectors. Tree-wide: a primitive in a new module is covered."""
    from membership_gate import generic_literal_field_offenders, literal_typed_field_members

    field_members = literal_typed_field_members()
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(generic_literal_field_offenders(tree, path, field_members=field_members))
    assert not offenders, (
        "a literal passed to a result primitive must be a member of its field's"
        " declared Literal, or the strict enum at response shaping rejects the whole"
        f" batch: {'; '.join(offenders)}"
    )


def test_generic_membership_detector_bites():
    """The generic detector fires on the historical regression shape, and only
    on it -- the proof that replacing the per-field list with a derived one did
    not silently turn the sweep into a no-op."""
    from membership_gate import generic_literal_field_offenders, literal_typed_field_members

    members = literal_typed_field_members()
    assert members, "no Literal fields were enumerated; the generic sweep is a no-op"
    bad = ast.parse('def f():\n    return _failed_engine_result(classification="totally_made_up")\n')
    hits = generic_literal_field_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", field_members=members)
    assert len(hits) == 1 and "totally_made_up" in hits[0], hits
    # A real member literal passes.
    good = ast.parse('def f():\n    return _failed_engine_result(classification="network_error")\n')
    assert not generic_literal_field_offenders(good, REPO_ROOT / "app" / "_synthetic.py", field_members=members)
    # A non-literal (the shape a correct caller writes) passes without comment.
    var = ast.parse("def f(c):\n    return _failed_engine_result(classification=c)\n")
    assert not generic_literal_field_offenders(var, REPO_ROOT / "app" / "_synthetic.py", field_members=members)


def test_generic_membership_subsumes_the_per_field_detectors():
    """The generic sweep covers every field the three per-field detectors
    guarded, and every primitive the hard-coded list forgot.

    The historical detectors matched on a *transcribed* primitive list of six
    names (``_RESULT_PRIMITIVES`` below). Three live primitives that set
    classification/status/strategy were never on it -- ``_binary_no_text_result``
    (which sets all three), ``_crawl_result_from_engine_result``, and
    ``_terminal_engine_result`` -- and neither were the direct dataclass
    constructions at five sites that bypass the primitives entirely. The
    derived registry has twelve entries and is closed by construction.

    This assert is the property that licenses deleting the per-field list: the
    coverage gap is not a matter of opinion, it is the set difference below.
    """
    from membership_gate import RESULT_DATACLASS_NAMES, RESULT_PRIMITIVES, literal_typed_field_members

    members = literal_typed_field_members()
    # The three escaped axes the per-field detectors were written for.
    assert "classification" in members and "site_error" in members["classification"]
    assert "status" in members and "failed" in members["status"]
    # The six-name list the per-field detectors transcribed.
    transcribed = {
        "_failed_engine_result",
        "_make_engine_result",
        "_engine_result_from_exception",
        "_skipped_engine_result",
        "_crawl_result_from_exception",
        "_terminal_crawl_result",
    }
    missed = RESULT_PRIMITIVES - transcribed
    assert missed == {
        "_binary_no_text_result",
        "_crawl_result_from_engine_result",
        "_terminal_engine_result",
    } | RESULT_DATACLASS_NAMES, (
        "the derived registry must cover the primitives and dataclasses the"
        f" transcribed list omitted: {sorted(missed)}"
    )


def test_result_construction_enforces_membership_at_the_dataclass():
    """A result dataclass built ANYWHERE rejects a non-member value.

    The historical escapes went through direct constructions outside
    result_helpers, so routing was policed geographically. That is no longer the
    load-bearing guarantee: every result dataclass validates its Literal fields
    in ``__post_init__``, so a build site cannot escape membership checking
    regardless of who calls it. This is the falsifiable version of that claim --
    a bad value at a direct construction site must raise, and a member must not.
    """
    from app.models import (
        CrawlResult,
        CrawlStrategyAttempt,
        EngineCrawlResult,
    )

    good_domain = "example.com"
    # A direct construction with all-member values is accepted: the gate must
    # not reject legitimate direct builds (tests and the primitives both do it).
    CrawlResult(
        url="http://a/",
        status="success",
        classification="rendered",
        final_url=None,
        html=None,
        text=None,
        attempts=1,
        elapsed_ms=0,
        domain=good_domain,
    )
    CrawlStrategyAttempt(
        strategy="BROWSER_HEADLESS",
        status="success",
        classification="rendered",
        http_status=200,
        content_type=None,
        elapsed_ms=0,
    )
    # A non-member on each Literal axis raises at construction.
    for kwargs, field in (
        ({"status": "Bogus"}, "status"),
        ({"classification": "Bogus"}, "classification"),
        ({"selected_strategy": "BROWSER_INVENTED"}, "selected_strategy"),
    ):
        with pytest.raises(ValueError, match=f".{field}="):
            EngineCrawlResult(
                original_url="http://a/",
                normalized_url="http://a/",
                final_url=None,
                status=kwargs.get("status", "success"),
                classification=kwargs.get("classification", "rendered"),
                selected_strategy=kwargs.get("selected_strategy"),
                attempted_strategies=[],
                http_status=None,
                headers={},
                content_type=None,
                html=None,
                text=None,
                extraction_method=None,
                quality_score=0.0,
            )
    # CrawlError axis: a stable code outside the declared taxonomy is rejected.
    with pytest.raises(ValueError, match=r"\.error="):
        CrawlResult(
            url="http://a/",
            status="failed",
            classification="network_error",
            final_url=None,
            html=None,
            text=None,
            attempts=1,
            elapsed_ms=0,
            domain=good_domain,
            error="worker_failed",
        )


# ===========================================================================
# NAMED-CONSTANT provenance (program 2)
#
# A literal detector is blind to a Name. The historical defect was exactly
# that shape: EXTRACTION_METHOD_BINARY_NO_TEXT -- a constant from the
# extraction-method namespace -- was assigned into a classification field. Its
# *value* ("binary_no_text") collides with a real classification member, so
# even resolving the name to its value cannot detect it: the collision is the
# defect. The signal that is actually wrong is the constant's NAMESPACE, so
# that is what the detector keys on.
# ===========================================================================


def test_result_primitives_take_taxonomy_constants_not_foreign_namespaces():
    """A constant from one taxonomy must not be consumed as a member of
    another. Value membership cannot see this -- the colliding value is what
    made the historical defect invisible."""
    from membership_gate import literal_typed_field_members, wrong_namespace_constant_offenders

    field_members = literal_typed_field_members()
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(wrong_namespace_constant_offenders(tree, path, field_members=field_members))
    assert not offenders, (
        "a constant from a foreign namespace was consumed as a taxonomy member; the"
        " value collides with a real member which is exactly why a value check misses"
        f" it: {'; '.join(offenders)}"
    )


def test_wrong_namespace_detector_bites():
    """The provenance detector fires on the historical shape and admits the
    correct one."""
    from membership_gate import literal_typed_field_members, wrong_namespace_constant_offenders

    members = literal_typed_field_members()
    bad = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f():\n"
        "    return _failed_engine_result(classification=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    hits = wrong_namespace_constant_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", field_members=members)
    assert len(hits) == 1 and "EXTRACTION_METHOD_" in hits[0], hits
    # The same constant used as its own field is the intended use.
    ok = ast.parse(
        'EXTRACTION_METHOD_BINARY_NO_TEXT = "binary_no_text"\n'
        "def f(m):\n"
        "    return _make_engine_result(extraction_method=EXTRACTION_METHOD_BINARY_NO_TEXT)\n"
    )
    assert not wrong_namespace_constant_offenders(ok, REPO_ROOT / "app" / "_synthetic.py", field_members=members)
    # A parameter (what a correct caller writes) is not a constant: passes.
    param = ast.parse("def f(cls):\n    return _failed_engine_result(classification=cls.site_error)\n")
    assert not wrong_namespace_constant_offenders(
        param, REPO_ROOT / "app" / "_synthetic.py", field_members=members
    )


# ---------------------------------------------------------------------------
# The runtime gate (program 1+2 closure): the class dies at the boundary
# ---------------------------------------------------------------------------


def test_the_membership_gate_rejects_a_non_member_value():
    """The AST detectors see source text; the historical escape reached the
    primitive through a local variable, which no textual detector can see.
    The gate is the structural closure: whatever indirection a value took, it
    must pass the primitive to become a result."""
    from membership_gate import NonMemberValue, assert_member, assert_optional_member

    members = frozenset({"rendered", "site_error"})
    assert_member("rendered", field="classification", members=members, where="_test")
    with pytest.raises(NonMemberValue):
        assert_member("binary_no_text", field="classification", members=members, where="_test")
    # None is admitted for the optional Literal fields.
    assert_optional_member(None, field="strategy", members=frozenset({"BROWSER_HEADLESS"}), where="_test")
    with pytest.raises(NonMemberValue):
        assert_optional_member("BROWSER_DECLARED_CRAWLER", field="strategy", members=frozenset({}), where="_test")


def test_the_gate_sees_the_local_variable_indirection_the_ast_detectors_cannot():
    """The exact historical shape, end to end: a local variable assigned a
    wrong-namespace constant, then handed to the primitive as a bare Name. No
    kwarg-level AST detector fires on this; the gate does, because it inspects
    the value at the boundary instead of the text at the call."""
    import asyncio

    from membership_gate import NonMemberValue, field_member_sets

    members = field_member_sets()
    classification_members = members["classification"]

    async def scenario() -> None:
        # `classification` is a local whose provenance no AST walk can resolve
        # from the call site alone.
        classification = "totally_made_up"
        from membership_gate import assert_result_fields

        assert_result_fields(
            {"classification": classification},
            where="http._http_strategy_result",
            members=members,
        )

    with pytest.raises(NonMemberValue):
        asyncio.run(scenario())
    # A real member through the same indirection passes.
    assert "binary_no_text" in classification_members


def test_the_gate_would_have_caught_the_shipped_defect():
    """``EXTRACTION_METHOD_BINARY_NO_TEXT`` has a value that IS a legal
    classification member -- so this is the shape a value check admits. The
    gate inspects the value, and the value here is legal, which is precisely
    the point: the *provenance* AST detector above is what names the defect,
    while the gate is what guarantees no wrong value of ANY provenance crosses.
    Both are needed and neither subsumes the other."""
    from membership_gate import NonMemberValue, assert_result_fields, field_member_sets

    members = field_member_sets()
    # A genuinely non-member value through the same local-variable shape.
    with pytest.raises(NonMemberValue):
        assert_result_fields(
            {"classification": "not_a_real_member"},
            where="http._http_strategy_result",
            members=members,
        )


# ===========================================================================
# ALIAS-BLINDNESS (program 3): detectors must resolve import bindings, not
# match on a literal module name
# ===========================================================================


def test_dynamic_pattern_re_calls_under_any_alias():
    """``import re as _re`` then ``_re.compile(<dynamic>)`` is invisible to a
    detector anchored on the name ``re``. The binding is resolved first, so an
    alias cannot rename a dynamic pattern past the chokepoint. This subsumes
    ``test_no_dynamic_pattern_re_calls`` and the call shape of
    ``test_no_bare_re_compile_on_nonliteral``."""
    from membership_gate import aliased_dynamic_pattern_re_offenders

    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(aliased_dynamic_pattern_re_offenders(tree, path))
    assert not offenders, (
        "re.<method> with a dynamic pattern must go through app.safe_regex, under any"
        f" import alias: {'; '.join(offenders)}"
    )


def test_alias_detector_bites():
    from membership_gate import aliased_dynamic_pattern_re_offenders

    aliased = ast.parse("import re as _re\n\n\ndef f(p):\n    return _re.compile(p)\n")
    hits = aliased_dynamic_pattern_re_offenders(aliased, REPO_ROOT / "app" / "_synthetic.py")
    assert len(hits) == 1 and "_re.compile" in hits[0], hits
    # A literal pattern under the same alias is safe and stays unflagged.
    literal = ast.parse('import re as _re\n\n\ndef f():\n    return _re.compile(r"<loc>")\n')
    assert not aliased_dynamic_pattern_re_offenders(literal, REPO_ROOT / "app" / "_synthetic.py")
    # The plain ``re`` name is still covered (subsumption of the old detector).
    plain = ast.parse("import re\n\n\ndef f(p):\n    return re.search(p, t)\n")
    assert aliased_dynamic_pattern_re_offenders(plain, REPO_ROOT / "app" / "_synthetic.py")
    # No re import at all: nothing to check.
    none_ = ast.parse("def f(p):\n    return compile(p)\n")
    assert not aliased_dynamic_pattern_re_offenders(none_, REPO_ROOT / "app" / "_synthetic.py")


def test_sanctioned_func_calls_under_any_alias():
    """A sanctioned chokepoint function is covered under any import alias.

    The name-matching detectors each carry a frozenset of spellings and were
    extended one alias at a time. This sweep resolves the import binding, so a
    new alias of ``decompress_body`` cannot name its way out of the cap
    requirement the way ``_re`` names its way out of the pattern check."""
    from membership_gate import aliased_sanctioned_func_offenders

    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(aliased_sanctioned_func_offenders(tree, path))
    assert not offenders, (
        "a sanctioned chokepoint function is called through an alias the"
        " name-matching detectors do not recognize; a missing cap or filter would"
        f" be invisible to them: {'; '.join(offenders)}"
    )


def test_sanctioned_func_alias_detector_bites():
    """The alias audit fires on a spelling the hand-kept list does not contain,
    and stays silent on the two spellings the existing list already covers."""
    from membership_gate import aliased_sanctioned_func_offenders

    unknown = ast.parse(
        "from app.crawl.decompression import decompress_body as dec\n\n"
        "def f(raw, enc):\n    return dec(raw, enc)\n"
    )
    hits = aliased_sanctioned_func_offenders(unknown, REPO_ROOT / "app" / "_synthetic.py")
    assert len(hits) == 1 and "decompress_body" in hits[0], hits
    # The two spellings _DECOMPRESS_FUNCS already lists are known and silent.
    known = ast.parse(
        "from app.crawl.decompression import decompress_body as _decompress_body\n\n"
        "def f(raw, enc):\n    return _decompress_body(raw, enc)\n"
    )
    assert not aliased_sanctioned_func_offenders(known, REPO_ROOT / "app" / "_synthetic.py")
    # An import that is not a sanctioned function is not this sweep's business.
    unrelated = ast.parse("from os import path as p\n\n\ndef f():\n    return p.join('a')\n")
    assert not aliased_sanctioned_func_offenders(unrelated, REPO_ROOT / "app" / "_synthetic.py")


# ===========================================================================
# TEST DOUBLES THAT SWALLOW SIGNATURE DRIFT (program 4)
# ===========================================================================


def test_doubles_do_not_accept_and_drop_kwargs():
    """A Protocol-shaped double that accepts ``**kwargs`` and never routes it
    is blind to a signature change on the production callable: the
    ``failed_result`` refactor broke 24 tests silently through exactly this
    shape, because the doubles accepted the new kwarg and dropped it."""
    from membership_gate import swallowing_test_double_offenders

    offenders: list[str] = []
    for path, tree in trees_under(TESTS_DIR):
        offenders.extend(swallowing_test_double_offenders(tree, path))
    assert not offenders, (
        "bind the production kwarg by name or forward **kwargs to the real callable; a"
        " double that drops it implements a contract that no longer exists: "
        + "; ".join(offenders)
    )


def test_swallowing_double_detector_bites():
    from membership_gate import swallowing_test_double_offenders

    drops = ast.parse(
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return {}\n"
    )
    hits = swallowing_test_double_offenders(drops, REPO_ROOT / "tests" / "_synthetic.py")
    assert len(hits) == 1 and "run(**kwargs)" in hits[0], hits
    # Forwarding is the sanctioned shape: the double passes the contract through.
    forwards = ast.parse(
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return real.run(jobs, worker, on_result=on_result, **kwargs)\n"
    )
    assert not swallowing_test_double_offenders(forwards, REPO_ROOT / "tests" / "_synthetic.py")
    # Consulting kwargs by name (kwargs.get) also counts as routing it.
    consults = ast.parse(
        "class _S:\n"
        "    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):\n"
        "        return kwargs.get('x', 1)\n"
    )
    assert not swallowing_test_double_offenders(consults, REPO_ROOT / "tests" / "_synthetic.py")
    # A non-Protocol method is not a double and stays unflagged.
    unrelated = ast.parse("def run(a, **kwargs):\n    return 1\n")
    assert not swallowing_test_double_offenders(unrelated, REPO_ROOT / "tests" / "_synthetic.py")


def test_no_existing_double_swallows_failed_result():
    """Derived from the production signature, not a hand-kept list: every
    scheduler double in tests/ binds ``failed_result`` by name today, which is
    the property the ``failed_result`` refactor needed. A double that regressed
    to ``**kwargs``-only would be caught by both this and the sweep above."""
    offenders: list[str] = []
    for path, tree in trees_under(TESTS_DIR):
        for node in ast.walk(tree):
            if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run"):
                continue
            positional = [a.arg for a in node.args.posonlyargs + node.args.args if a.arg not in {"self", "cls"}]
            if len(positional) < 2:
                continue
            names = {a.arg for a in node.args.args + node.args.kwonlyargs}
            names |= {a.arg for a in node.args.posonlyargs}
            if "failed_result" not in names:
                offenders.append(offender(path, node.lineno, "run() does not bind failed_result by name"))
    assert not offenders, (
        "the DomainScheduler.run contract requires failed_result; a double that omits it"
        " silently drops the envelope the pipeline supplies: " + "; ".join(offenders)
    )


# ===========================================================================
# Program 5 (hunt): the error= field's undeclared taxonomy
# ===========================================================================


def test_error_field_values_are_classification_members():
    """A literal ``error=`` on a result primitive is in the declared taxonomy.

    ``error`` is a declared ``CrawlError`` Literal: page-outcome codes that are
    also Classification members, plus pipeline conditions with no page analogue
    (capacity, config). The field is documented as carrying a stable code
    (render.py) that callers key on, so a literal outside the declared taxonomy
    is a contract leak; this reports it so the missing chokepoint is visible
    rather than latent.
    """
    import typing

    from membership_gate import error_field_taxonomy_offenders

    from app.models import CrawlError

    members = frozenset(typing.get_args(CrawlError))
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(error_field_taxonomy_offenders(tree, path, members=members))
    assert not offenders, (
        "error= must be a member of the declared CrawlError taxonomy (the field is"
        f" documented as a stable code callers key on): {'; '.join(offenders)}"
    )


def test_error_field_taxonomy_detector_bites():
    """The hunt detector fires on the shipped shape and admits the classified one."""
    import typing

    from membership_gate import error_field_taxonomy_offenders

    from app.models import CrawlError

    members = frozenset(typing.get_args(CrawlError))
    bad = ast.parse('def f():\n    return _terminal_crawl_result(url="u", error="worker_failed")\n')
    hits = error_field_taxonomy_offenders(bad, REPO_ROOT / "app" / "_synthetic.py", members=members)
    assert len(hits) == 1 and "worker_failed" in hits[0], hits
    # A member literal passes.
    good = ast.parse('def f():\n    return _terminal_crawl_result(url="u", error="site_error")\n')
    assert not error_field_taxonomy_offenders(good, REPO_ROOT / "app" / "_synthetic.py", members=members)
    # A classified variable is the sanctioned shape and passes.
    var = ast.parse("def f(c):\n    return _terminal_crawl_result(url='u', error=c)\n")
    assert not error_field_taxonomy_offenders(var, REPO_ROOT / "app" / "_synthetic.py", members=members)


def _job_engine_payload_chokepoint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    relative = path.relative_to(REPO_ROOT).as_posix()
    has_builder = any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "_job_engine_payload"
        for node in ast.walk(tree)
    )
    if relative != "app/api/job_runner.py" and not has_builder:
        return []
    execute_names = {"_execute_crawl", "_real_execute_crawl"}
    builder_names = {"_job_engine_payload"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign | ast.AnnAssign):
            continue
        value = node.value
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if isinstance(value, ast.Name) and value.id in execute_names:
            execute_names.update(target.id for target in targets if isinstance(target, ast.Name))
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in builder_names
        ):
            builder_names.update(target.id for target in targets if isinstance(target, ast.Name))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in execute_names):
            continue
        payload = node.args[0] if node.args else next(
            (keyword.value for keyword in node.keywords if keyword.arg in {"payload", "data"}), None
        )
        if isinstance(payload, ast.Name) and payload.id in builder_names:
            continue
        if (
            isinstance(payload, ast.Call)
            and isinstance(payload.func, ast.Name)
            and payload.func.id in builder_names
        ):
            continue
        offenders.append(offender(path, node.lineno, "_execute_crawl payload bypasses _job_engine_payload"))
    return offenders


def test_job_engine_payload_uses_single_builder():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(_job_engine_payload_chokepoint_offenders(tree, path))
    assert not offenders, "Build whole-site engine payloads only with _job_engine_payload: " + ", ".join(offenders)


def test_job_engine_payload_chokepoint_detector_bites():
    pre_fix = ast.parse(
        "async def run(job, batch):\n"
        "    payload = {'urls': batch, 'policy': dict(job.policy)}\n"
        "    await _execute_crawl(payload)\n"
    )
    alias = ast.parse(
        "async def run(job, batch):\n"
        "    execute = _execute_crawl\n"
        "    await execute({'urls': batch, 'policy': {}})\n"
    )
    path = REPO_ROOT / "app" / "api" / "job_runner.py"
    assert _job_engine_payload_chokepoint_offenders(pre_fix, path)
    assert _job_engine_payload_chokepoint_offenders(alias, path)
    good = ast.parse(
        "async def run(job, batch):\n"
        "    payload = _job_engine_payload(job, batch)\n"
        "    await _execute_crawl(payload)\n"
    )
    assert not _job_engine_payload_chokepoint_offenders(good, path)


def _frontier_expression(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "frontier"


def _direct_frontier_growth_offenders(
    tree: ast.AST,
    path: pathlib.Path,
    *,
    allowed_functions: frozenset[str] = frozenset(),
) -> list[str]:
    growth_methods = {"append", "extend", "insert"}
    offenders: list[str] = []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def is_allowed(node: ast.AST) -> bool:
        if not allowed_functions:
            return False
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = parents.get(owner)
        if not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef) or owner.name not in allowed_functions:
            return False
        if owner.name == "normalize_job_state":
            return (
                isinstance(node, ast.Assign)
                and any(_frontier_expression(target) for target in node.targets)
                and isinstance(node.value, ast.Name)
                and node.value.id == "frontier"
            )
        return (
            owner.name == "_persist"
            and isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and _frontier_expression(node.value)
        )

    for node in ast.walk(tree):
        if is_allowed(node):
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in growth_methods and _frontier_expression(node.func.value):
                offenders.append(
                    offender(path, node.lineno, "direct frontier growth bypasses admit_job_links")
                )
        elif isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_frontier_expression(target) for target in targets):
                offenders.append(
                    offender(path, node.lineno, "frontier replacement bypasses admit_job_links")
                )
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
                if _frontier_expression(node.value):
                    offenders.append(
                        offender(path, node.lineno, "frontier alias bypasses admit_job_links")
                    )
                if node.value.attr == "append" and _frontier_expression(node.value.value):
                    offenders.append(
                        offender(path, node.lineno, "frontier method alias bypasses admit_job_links")
                    )
        elif isinstance(node, ast.Subscript) and _frontier_expression(node.value):
            parent = parents.get(node)
            if isinstance(parent, ast.Assign | ast.AugAssign | ast.AnnAssign):
                offenders.append(
                    offender(path, node.lineno, "frontier slice mutation bypasses admit_job_links")
                )
    return offenders


def test_job_frontier_growth_only_uses_funnel():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        if path.relative_to(REPO_ROOT).as_posix() == "app/crawl/admission.py":
            continue
        allowed = (
            frozenset({"normalize_job_state", "_persist"})
            if path.relative_to(REPO_ROOT).as_posix() == "app/crawl/job_store.py"
            else frozenset()
        )
        offenders.extend(_direct_frontier_growth_offenders(tree, path, allowed_functions=allowed))
    assert not offenders, "Grow whole-site frontiers only through admit_job_links: " + ", ".join(offenders)


def test_job_frontier_growth_detector_bites():
    path = REPO_ROOT / "app" / "api" / "job_runner.py"
    pre_fix_owners = (
        "def run(job, url):\n    job.frontier.append(url)\n",
        "def run(job, url):\n    append = job.frontier.append\n    append(url)\n",
        "def run(job, url):\n    job.frontier += [url]\n",
        "def run(job, urls):\n    job.frontier = urls\n",
    )
    for source in pre_fix_owners:
        assert _direct_frontier_growth_offenders(ast.parse(source), path), source
    alias = ast.parse(
        "def run(job, url):\n"
        "    frontier = job.frontier\n"
        "    frontier.append(url)\n"
    )
    assert _direct_frontier_growth_offenders(alias, path)
    good = ast.parse("async def run(job, links):\n    await admit_job_links(job, links)\n")
    assert not _direct_frontier_growth_offenders(good, path)


_FILTER_NAMES = {
    "app.constants.is_analytics_host": "is_analytics_host",
    "app.constants.is_document_url": "is_document_url",
    "app.crawl.scope_match.scope_allows": "scope_allows",
    "app.validation.normalize_public_http_url": "normalize_public_http_url",
}


def _imported_filter_names(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {
            "app.constants",
            "app.crawl.scope_match",
            "app.validation",
        }:
            for imported in node.names:
                canonical = f"{node.module}.{imported.name}"
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
        elif isinstance(node, ast.Import):
            for imported in node.names:
                canonical = imported.name
                if canonical in _FILTER_NAMES:
                    aliases[imported.asname or imported.name] = _FILTER_NAMES[canonical]
    return aliases


def _shared_admission_filter_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    assert isinstance(tree, ast.Module)
    aliases = _imported_filter_names(tree)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    function_calls: dict[ast.FunctionDef | ast.AsyncFunctionDef, set[str]] = {}
    filter_names = set(_FILTER_NAMES.values())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        name = aliases.get(node.func.id, node.func.id)
        if name not in filter_names:
            continue
        owner = parents.get(node)
        while owner is not None and not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            owner = parents.get(owner)
        if isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
            function_calls.setdefault(owner, set()).add(name)
    offenders: list[str] = []
    for function, names in function_calls.items():
        if len(names) >= 2:
            offenders.append(
                offender(path, function.lineno, "recursive admission filter list is re-inlined")
            )
    return offenders


def test_shared_admission_filter_set_has_no_twin_list():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        if path.relative_to(REPO_ROOT).as_posix() == "app/crawl/admission.py":
            continue
        offenders.extend(_shared_admission_filter_offenders(tree, path))
    assert not offenders, "Use the shared admission filter chokepoint: " + ", ".join(offenders)


def test_shared_admission_filter_detector_bites():
    pre_fix = ast.parse(
        "def expand(links, visited, scope):\n"
        "    candidates = []\n"
        "    for link in links:\n"
        "        if link.url in visited: continue\n"
        "        if is_analytics_host(link.url): continue\n"
        "        if is_document_url(link.url): continue\n"
        "        if scope is not None and not scope_allows(scope, link.url): continue\n"
        "        candidates.append(link)\n"
        "    return candidates\n"
    )
    path = REPO_ROOT / "app" / "_synthetic.py"
    assert _shared_admission_filter_offenders(pre_fix, path)
    good = ast.parse(
        "async def expand(links):\n"
        "    return await admit_discovered_links(links)\n"
    )
    assert not _shared_admission_filter_offenders(good, path)


def test_job_frontier_detector_sanction_is_narrow():
    path = REPO_ROOT / "app" / "crawl" / "job_store.py"
    allowed = frozenset({"normalize_job_state", "_persist"})
    bad = ast.parse("def grow_frontier(job):\n    job.frontier.append('x')\n")
    assert _direct_frontier_growth_offenders(bad, path, allowed_functions=allowed)
    sanctioned = ast.parse("def normalize_job_state(job):\n    frontier = []\n    job.frontier = frontier\n")
    assert not _direct_frontier_growth_offenders(sanctioned, path, allowed_functions=allowed)


_DEFINITION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_REQUIRED_PROPERTY_FINGERPRINTS = {
    "public-target": frozenset({"ipaddress_range_verdict"}),
    "bool-env": frozenset({"bool_literal_set"}),
    "robots-policy": frozenset(
        {"robots_url", "parse_robots_rules_call", "parse_robots_rules_definition", "robots_bounded_ttl_cache"}
    ),
}


def _property(name: str) -> Property:
    return PROPERTY_BY_NAME[name]


def _property_contexts(tree: ast.AST) -> list[ast.AST]:
    symbol_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
    contexts: list[ast.AST] = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for scope in (tree, *(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef))):
        contexts.extend(node for node in getattr(scope, "body", []) if not isinstance(node, symbol_types))
    return contexts


def _local_nodes(node: ast.AST):
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from _local_nodes(child)


def _module_name(path: pathlib.Path) -> str:
    relative = _relative_app_path(path)
    if relative.endswith("/__init__.py"):
        return relative[: -len("/__init__.py")].replace("/", ".")
    if relative.endswith(".py"):
        return relative[:-3].replace("/", ".")
    return relative.replace("/", ".")


_PROPERTY_MODULES: ContextVar[tuple[tuple[pathlib.Path, ast.AST], ...] | None] = ContextVar(
    "property_modules", default=None
)


def _module_aware(detector):
    def wrapped(tree, path, prop, *, modules=None):
        token = _PROPERTY_MODULES.set(modules)
        try:
            return detector(tree, path, prop)
        finally:
            _PROPERTY_MODULES.reset(token)

    return wrapped


def _module_definition_index(
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None,
) -> dict[str, list[tuple[pathlib.Path, ast.AST, ast.AST]]]:
    index: dict[str, list[tuple[pathlib.Path, ast.AST, ast.AST]]] = {}
    for path, tree in modules or ():
        module = _module_name(path)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(f"{module}.{node.name}", []).append((path, tree, node))
            elif isinstance(node, ast.ClassDef):
                index.setdefault(f"{module}.{node.name}", []).append((path, tree, node))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.setdefault(f"{module}.{node.name}.{child.name}", []).append((path, tree, child))
    return index


def _resolved_local_nodes(
    tree: ast.AST,
    node: ast.AST,
    excluded_ids: set[int] | frozenset[int] = frozenset(),
    *,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None = None,
    prop: Property | None = None,
) -> list[tuple[ast.AST, ast.AST]]:
    definitions: dict[str, list[ast.AST]] = {}
    for candidate in ast.walk(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(candidate.name, []).append(candidate)
    module_index = _module_definition_index(modules)
    resolved: list[tuple[ast.AST, ast.AST]] = []
    seen: set[tuple[int, int]] = set()
    pending: list[tuple[ast.AST, ast.AST]] = [(tree, node)]
    while pending:
        current_tree, current = pending.pop()
        current_key = (id(current_tree), id(current))
        if current_key in seen or id(current) in excluded_ids:
            continue
        seen.add(current_key)
        resolved.append((current_tree, current))
        for child in _local_nodes(current):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                matches = definitions.get(child.func.id, [])
                if len(matches) == 1 and id(matches[0]) not in excluded_ids:
                    pending.append((current_tree, matches[0]))
            if not module_index:
                continue
            matches = module_index.get(resolved_call_name(current_tree, child), [])
            if len(matches) != 1:
                continue
            candidate_path, candidate_tree, candidate = matches[0]
            if (
                prop is not None
                and _relative_app_path(candidate_path) == prop.owner_module
                and candidate.name in prop.owner_symbols
            ):
                continue
            pending.append((candidate_tree, candidate))
    return resolved


def _relative_app_path(path: pathlib.Path) -> str:
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _owner_definition_ids(tree: ast.AST, path: pathlib.Path, prop: Property) -> set[int]:
    if _relative_app_path(path) != prop.owner_module:
        return set()
    by_symbol: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, _DEFINITION_TYPES) and node.name in prop.owner_symbols:
            by_symbol.setdefault(node.name, []).append(node)
    owner_ids: set[int] = set()
    for nodes in by_symbol.values():
        owner_ids.add(id(nodes[0]))
    return owner_ids


def _property_owner(
    path: pathlib.Path,
    prop: Property,
    symbol: str | None = None,
    *,
    tree: ast.AST | None = None,
    node: ast.AST | None = None,
) -> bool:
    if node is not None and tree is not None:
        return id(node) in _owner_definition_ids(tree, path, prop)
    return False


def _property_suppressed(prop: Property, path: pathlib.Path, symbol: str, fingerprint: str) -> bool:
    relative = _relative_app_path(path)
    return any(
        entry.module == relative and entry.symbol == symbol and entry.fingerprint == fingerprint
        for entry in prop.suppressions
    )


def _property_nodes(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[tuple[str, ast.AST]]:
    owner_ids = _owner_definition_ids(tree, path, prop)
    return [
        (getattr(node, "name", "<module>"), node)
        for node in _property_contexts(tree)
        if id(node) not in owner_ids
    ]


_PUBLIC_RANGE_ATTRIBUTES = frozenset(
    {"is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified"}
)


def _public_range_accesses(nodes: list[tuple[ast.AST, ast.AST]]) -> int:
    count = 0
    for owner_tree, node in nodes:
        for child in _local_nodes(node):
            if isinstance(child, ast.Attribute) and child.attr in _PUBLIC_RANGE_ATTRIBUTES:
                count += 1
            elif isinstance(child, ast.Call):
                name = resolved_call_name(owner_tree, child)
                if name.endswith(".ip_network"):
                    count += 1
                elif (
                    name.endswith(tuple(f".{attribute}" for attribute in _PUBLIC_RANGE_ATTRIBUTES))
                    and len(child.args) >= 2
                ):
                    count += 1
    return count


def _public_target_counts(nodes: list[tuple[ast.AST, ast.AST]]) -> tuple[int, int]:
    parses = sum(
        resolved_call_name(owner_tree, call) == "ipaddress.ip_address"
        for owner_tree, node in nodes
        for call in _local_nodes(node)
        if isinstance(call, ast.Call)
    )
    return parses, _public_range_accesses(nodes)


def _public_target_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    observations: dict[str, int] = {}

    if _relative_app_path(path) == prop.owner_module:
        owner_ids = _owner_definition_ids(tree, path, prop)
        owner_nodes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) in owner_ids
        ]
        owner_parses, owner_ranges = _public_target_counts([(tree, node) for node in owner_nodes])
        if owner_parses and owner_ranges:
            observations["ipaddress_range_verdict"] = min(owner_parses, owner_ranges)
        return observations
    excluded = _owner_definition_ids(tree, path, prop)
    for _name, node in _property_nodes(tree, path, prop):
        parses, ranges = _public_target_counts(
            _resolved_local_nodes(tree, node, excluded, modules=None, prop=prop)
        )
        count = min(parses, ranges) if parses and ranges else 0
        if count:
            observations["ipaddress_range_verdict"] = observations.get("ipaddress_range_verdict", 0) + count
    return observations


@_module_aware
def _public_target_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    excluded = _owner_definition_ids(tree, path, prop)
    for name, node in _property_nodes(tree, path, prop):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        nodes = _resolved_local_nodes(
            tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop
        )
        parses, ranges = _public_target_counts(nodes)
        if parses and ranges:
            offenders.append(offender(path, getattr(node, "lineno", 1), f"{name} reimplements public-address policy"))
    return offenders


_BOOL_ENV_SPELLINGS = frozenset({"0", "1", "false", "true", "no", "yes", "off", "on"})


def _bool_literal_values(node: ast.AST) -> set[str]:
    elements = node.keys if isinstance(node, ast.Dict) else getattr(node, "elts", ())
    return {
        element.value
        for element in elements
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }


def _bool_named_sets(nodes) -> dict[str, set[str]]:
    named: dict[str, set[str]] = {}
    for assignment in nodes:
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
            assignment.value, (ast.Set, ast.List, ast.Tuple, ast.Dict)
        ):
            continue
        literals = _bool_literal_values(assignment.value)
        if not literals & _BOOL_ENV_SPELLINGS:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        for target in targets:
            if isinstance(target, ast.Name):
                named[target.id] = literals
    return named


def _bool_env_literal_sets(
    nodes: list[tuple[ast.AST, ast.AST]],
) -> tuple[dict[int, set[str]], dict[str, set[str]]]:
    direct: dict[int, set[str]] = {}
    local_nodes = [child for _owner_tree, node in nodes for child in _local_nodes(node)]
    for child in local_nodes:
        if isinstance(child, (ast.Set, ast.List, ast.Tuple, ast.Dict)) and _bool_literal_values(
            child
        ) & _BOOL_ENV_SPELLINGS:
            direct[id(child)] = _bool_literal_values(child)
    return direct, _bool_named_sets(local_nodes)


def _bool_env_tree_sets(
    tree: ast.AST,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None = None,
) -> dict[str, set[str]]:
    trees = (tree,) if modules is None else tuple(module_tree for _path, module_tree in modules)
    named: dict[str, set[str]] = {}
    for module_tree in trees:
        named.update(_bool_named_sets(ast.walk(module_tree)))
    return named


def _bool_env_node_observation(
    tree: ast.AST,
    nodes: list[tuple[ast.AST, ast.AST]],
    tree_sets: dict[str, set[str]] | None = None,
) -> tuple[bool, int]:
    direct, named = _bool_env_literal_sets(nodes)
    named = {**tree_sets, **named} if tree_sets else named
    local_nodes_with_tree = [
        (owner_tree, child)
        for owner_tree, node in nodes
        for child in _local_nodes(node)
    ]
    has_normalization = any(
        (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in {"strip", "lower", "casefold"}
        )
        or (
            isinstance(child, ast.Call)
            and resolved_call_name(owner_tree, child) == "getattr"
            and len(child.args) >= 2
            and isinstance(child.args[1], ast.Constant)
            and child.args[1].value in {"strip", "lower", "casefold"}
        )
        for owner_tree, child in local_nodes_with_tree
    )
    if not has_normalization:
        return False, 0
    count = 0
    for _owner_tree, child in local_nodes_with_tree:
        if not isinstance(child, ast.Compare) or not any(isinstance(op, (ast.In, ast.NotIn)) for op in child.ops):
            continue
        if any(
            id(part) in direct
            or (isinstance(part, ast.Name) and part.id in named)
            or (
                isinstance(part, ast.Attribute)
                and part.attr == "keys"
                and isinstance(part.value, ast.Name)
                and part.value.id in named
            )
            for part in _local_nodes(child)
        ):
            count += 1
    return True, count


def _bool_env_node_count(
    tree: ast.AST,
    nodes: list[tuple[ast.AST, ast.AST]],
    tree_sets: dict[str, set[str]] | None = None,
) -> int:
    return _bool_env_node_observation(tree, nodes, tree_sets)[1]


def _bool_env_contexts(
    tree: ast.AST,
    path: pathlib.Path,
    prop: Property,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None,
) -> list[list[tuple[ast.AST, ast.AST]]]:
    excluded = _owner_definition_ids(tree, path, prop)
    if _relative_app_path(path) == prop.owner_module:
        return [
            [(tree, node)]
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) in excluded
        ]
    return [
        _resolved_local_nodes(tree, node, excluded, modules=modules, prop=prop)
        for _name, node in _property_nodes(tree, path, prop)
    ]


def _bool_env_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    tree_sets = _bool_env_tree_sets(tree, None)
    contexts = _bool_env_contexts(tree, path, prop, None)
    observations_for_contexts = [
        _bool_env_node_observation(tree, context, tree_sets) for context in contexts
    ]
    if not any(observed for observed, _count in observations_for_contexts):
        return {}
    return {"bool_literal_set": sum(count for _observed, count in observations_for_contexts)}


@_module_aware
def _bool_env_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    tree_sets = _bool_env_tree_sets(tree, _PROPERTY_MODULES.get())
    excluded = _owner_definition_ids(tree, path, prop)
    for name, node in _property_nodes(tree, path, prop):
        context = _resolved_local_nodes(
            tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop
        )
        if _bool_env_node_count(tree, context, tree_sets):
            offenders.append(offender(path, getattr(node, "lineno", 1), f"{name} reimplements boolean environment parsing"))
    return offenders




def _source_and_literals(node: ast.AST) -> tuple[str, str]:
    nodes = list(_local_nodes(node))
    source = " ".join(ast.unparse(child) for child in nodes[1:]).lower() if isinstance(
        node, (ast.FunctionDef, ast.AsyncFunctionDef)
    ) else ast.unparse(node).lower()
    literals = "".join(
        child.value
        for child in nodes
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ).lower()
    return source, literals


def _robot_signal(node: ast.AST) -> bool:
    source, literals = _source_and_literals(node)
    return any(token in source or token in literals for token in ("robot", "crawl-delay", "disallow", "user-agent"))


def _call_leaf_names(tree: ast.AST, node: ast.AST):
    for child in _local_nodes(node):
        if isinstance(child, ast.Call):
            yield resolved_call_name(tree, child).rsplit(".", 1)[-1].lower()


def _fetch_like(tree: ast.AST, node: ast.AST) -> bool:
    return any(
        any(token in name for token in ("fetch", "request", "download", "retrieve", "http"))
        for name in _call_leaf_names(tree, node)
    )


def _string_constructor(tree: ast.AST, node: ast.AST) -> bool:
    for child in _local_nodes(node):
        if not isinstance(child, ast.Call):
            continue
        name = resolved_call_name(tree, child).rsplit(".", 1)[-1].lower()
        if name in {"format", "join"}:
            return True
        if (
            name == "getattr"
            and len(child.args) >= 2
            and isinstance(child.args[1], ast.Constant)
            and child.args[1].value in {"format", "join"}
        ):
            return True
    return False


def _url_literal_capability(tree: ast.AST, node: ast.AST) -> bool:
    if not isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)):
        return False
    nodes = list(_local_nodes(node))
    if not any(isinstance(child, (ast.BinOp, ast.JoinedStr)) for child in nodes) and not _string_constructor(
        tree, node
    ):
        return False
    parts = [
        child.value
        for child in nodes
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]
    return any("robot" in part or ".txt" in part for part in parts) and any(
        part.lstrip("{}").startswith("/") or "http" in part for part in parts
    )


def _url_constructor(tree: ast.AST, node: ast.AST) -> bool:
    nodes = list(_local_nodes(node))
    return any(
        isinstance(child, ast.Call)
        and resolved_call_name(tree, child).rsplit(".", 1)[-1].lower() in {"urljoin", "urlunsplit"}
        for child in nodes
    ) or any(_url_literal_capability(tree, child) for child in nodes)


def _robots_fetch_like(tree: ast.AST, node: ast.AST) -> bool:
    return any(
        "http" in name
        or name.startswith(("load_", "retrieve", "request"))
        or name == "fetch_and_decode"
        or (name in {"get", "open", "read", "send", "urlopen"} and _robot_signal(node))
        for name in _call_leaf_names(tree, node)
    )


def _parser_call_count(tree: ast.AST, node: ast.AST) -> int:
    count = 0
    for child in _local_nodes(node):
        if not isinstance(child, ast.Call):
            continue
        name = resolved_call_name(tree, child).rsplit(".", 1)[-1].lower()
        if (
            ("parse" in name and any(token in name for token in ("robot", "rule", "directive", "crawl")))
            or ("decode" in name and "rule" in name)
            or ("read" in name and "robot" in name)
        ):
            count += 1
    return count


def _parser_definition(tree: ast.AST, path: pathlib.Path, node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    _source, _literals = _source_and_literals(node)
    name = node.name.lower()
    if _relative_app_path(path) == "app/crawl/robots_parse.py" and ("robot" in name or "parse" in name):
        return True
    if "robot" in name and any(token in name for token in ("parse", "rule", "directive")):
        return True
    nodes = list(_local_nodes(node))
    line_scan = any(
        isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr in {"splitlines", "partition"}
        for child in nodes
    )
    collection_return = any(
        isinstance(child, ast.Return)
        and isinstance(child.value, (ast.ListComp, ast.List, ast.Tuple, ast.Dict, ast.DictComp))
        for child in nodes
    )
    return line_scan and collection_return


def _cache_call_count(tree: ast.AST, node: ast.AST) -> int:
    return sum(
        isinstance(child, ast.Call)
        and resolved_call_name(tree, child).rsplit(".", 1)[-1].lower().endswith("boundedttlcache")
        for child in _local_nodes(node)
    )


def _robots_observations_for_node(tree: ast.AST, path: pathlib.Path, node: ast.AST) -> dict[str, int]:
    observations: dict[str, int] = {}
    parser_calls = _parser_call_count(tree, node)
    parser_definitions = int(_parser_definition(tree, path, node))
    cache_calls = _cache_call_count(tree, node)
    url_calls = int(_url_constructor(tree, node))
    if url_calls and _robot_signal(node) and _robots_fetch_like(tree, node):
        observations["robots_url"] = url_calls
    if parser_calls:
        observations["parse_robots_rules_call"] = parser_calls
    if parser_definitions:
        observations["parse_robots_rules_definition"] = parser_definitions
    if cache_calls and (_robot_signal(node) or parser_calls or _fetch_like(tree, node)):
        observations["robots_bounded_ttl_cache"] = cache_calls
    return observations


def _robot_module_paths(
    tree: ast.AST,
    path: pathlib.Path,
    modules: tuple[tuple[pathlib.Path, ast.AST], ...] | None,
) -> dict[int, pathlib.Path]:
    module_paths = {id(module_tree): module_path for module_path, module_tree in modules or ()}
    module_paths.setdefault(id(tree), path)
    return module_paths


def _robot_capabilities_from_nodes(
    tree: ast.AST,
    path: pathlib.Path,
    prop: Property,
    nodes: list[tuple[ast.AST, ast.AST]],
    module_paths: dict[int, pathlib.Path],
    *,
    suppress_shared: bool,
) -> dict[str, int]:
    capabilities: dict[str, int] = {}
    for owner_tree, resolved in nodes:
        owner_path = module_paths.get(id(owner_tree), path)
        for capability, count in _robots_observations_for_node(owner_tree, owner_path, resolved).items():
            if suppress_shared and resolved is not nodes[0][1] and _robots_shared_parser(
                prop, owner_path, resolved, owner_tree, capability
            ):
                continue
            capabilities[capability] = capabilities.get(capability, 0) + count
    return capabilities


def _robots_policy_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    observations: dict[str, int] = {}
    contexts = (
        _property_contexts(tree)
        if _relative_app_path(path) == prop.owner_module
        else [node for _name, node in _property_nodes(tree, path, prop)]
    )
    module_paths = _robot_module_paths(tree, path, None)
    for node in contexts:
        resolved = _resolved_local_nodes(tree, node, modules=None, prop=prop)
        for fingerprint, count in _robot_capabilities_from_nodes(
            tree, path, prop, resolved, module_paths, suppress_shared=False
        ).items():
            observations[fingerprint] = observations.get(fingerprint, 0) + count
    return observations


def _robots_shared_parser(
    prop: Property,
    path: pathlib.Path,
    node: ast.AST,
    tree: ast.AST,
    capability: str,
) -> bool:
    return (
        capability == "parse_robots_rules_definition"
        and _relative_app_path(path) != prop.owner_module
        and _parser_definition(tree, path, node)
        and not _robots_fetch_like(tree, node)
        and not _url_constructor(tree, node)
        and not _cache_call_count(tree, node)
    )


@_module_aware
def _robots_policy_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    excluded = _owner_definition_ids(tree, path, prop)
    module_paths = _robot_module_paths(tree, path, _PROPERTY_MODULES.get())
    for name, node in _property_nodes(tree, path, prop):
        nodes = _resolved_local_nodes(
            tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop
        )
        capabilities = _robot_capabilities_from_nodes(
            tree, path, prop, nodes, module_paths, suppress_shared=True
        )
        if "robots_url" not in capabilities and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            builders = {
                resolved.name
                for _owner_tree, resolved in nodes
                if isinstance(resolved, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _url_constructor(_owner_tree, resolved)
                and _robot_signal(resolved)
                and not _robots_fetch_like(_owner_tree, resolved)
            }
            local_calls = {
                child.func.id
                if isinstance(child.func, ast.Name)
                else resolved_call_name(tree, child).rsplit(".", 1)[-1]
                for child in _local_nodes(node)
                if isinstance(child, ast.Call)
            }
            if builders & local_calls and _robots_fetch_like(tree, node):
                capabilities["robots_url"] = 1
        for capability in sorted(capabilities):
            if _property_suppressed(prop, path, name, capability):
                continue
            offenders.append(offender(path, getattr(node, "lineno", 1), f"{name} has robots capability {capability}"))
    return offenders


def _property_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    if prop.name == "public-target":
        return _public_target_observations(tree, path, prop)
    if prop.name == "bool-env":
        return _bool_env_observations(tree, path, prop)
    if prop.name == "robots-policy":
        return _robots_policy_observations(tree, path, prop)
    return {}


def _property_registry_failures(
    registry: tuple[Property, ...] = PROPERTY_REGISTRY,
    *,
    trees: tuple[tuple[pathlib.Path, ast.AST], ...] | None = None,
) -> list[str]:
    failures: list[str] = []
    names = [prop.name for prop in registry]
    if len(names) != len(set(names)):
        failures.append("property registry names are not unique")
    observed: dict[str, dict[str, int]] = {prop.name: {} for prop in registry}
    source_trees = app_trees(exclude=frozenset()) if trees is None else trees
    for path, tree in source_trees:
        for prop in registry:
            for fingerprint, count in _property_observations(tree, path, prop).items():
                observed[prop.name][fingerprint] = observed[prop.name].get(fingerprint, 0) + count
    for prop in registry:
        if not prop.fingerprints:
            failures.append(f"{prop.name} has no fingerprints")
        required = _REQUIRED_PROPERTY_FINGERPRINTS.get(prop.name)
        if required is None:
            failures.append(f"{prop.name} has no detector")
        else:
            missing = required - set(prop.fingerprints)
            if missing:
                failures.append(f"{prop.name} is missing required fingerprints: {sorted(missing)}")
        unobserved = set(prop.fingerprints) - set(observed[prop.name])
        if unobserved:
            failures.append(f"{prop.name} has unobserved fingerprints (dead configuration): {sorted(unobserved)}")
        zero_observations = {
            fingerprint
            for fingerprint in prop.fingerprints
            if fingerprint in observed[prop.name] and observed[prop.name][fingerprint] <= 0
        }
        if zero_observations:
            failures.append(
                f"{prop.name} has zero observations for fingerprints: {sorted(zero_observations)}"
            )
    return failures


def _observed_property_count(prop: Property) -> int:
    count = 0
    for path, tree in app_trees(exclude=frozenset()):
        count += sum(_property_observations(tree, path, prop).values())
    return count


def test_property_registry_is_not_empty():
    assert PROPERTY_REGISTRY


def _property_owner_failures(
    registry: tuple[Property, ...],
    definitions: dict[str, list[tuple[str, ast.AST]]],
) -> list[str]:
    failures: list[str] = []
    for prop in registry:
        for symbol in prop.owner_symbols:
            matches = definitions.get(symbol, [])
            if len(matches) != 1:
                failures.append(f"{symbol}={len(matches)}")
                continue
            relative, _node = matches[0]
            if relative != prop.owner_module:
                failures.append(f"{symbol}@{relative}")
    return failures


def test_property_registry_owners_exist():
    assert PROPERTY_REGISTRY
    assert all(prop.reason.strip() for prop in PROPERTY_REGISTRY)
    assert all(entry.reason.strip() for prop in PROPERTY_REGISTRY for entry in prop.suppressions)
    for prop in PROPERTY_REGISTRY:
        site_module, site_symbol = prop.definition_site.split(":", 1)
        assert site_module == prop.owner_module
        assert site_symbol in prop.owner_symbols
    definitions: dict[str, list[tuple[str, ast.AST]]] = {}
    for path, tree in app_trees(exclude=frozenset()):
        relative = _relative_app_path(path)
        for node in ast.walk(tree):
            if isinstance(node, _DEFINITION_TYPES):
                definitions.setdefault(node.name, []).append((relative, node))
    failures = _property_owner_failures(PROPERTY_REGISTRY, definitions)
    assert not failures, "property registry owners are not unique: " + ", ".join(failures)


def test_property_registry_fingerprints_are_observed():
    failures = _property_registry_failures()
    assert not failures, "; ".join(failures)


def test_property_registry_detector_bites():
    prop = _property("bool-env")
    path = REPO_ROOT / "app" / "_zero_observation.py"
    tree = ast.parse("def normalize(value):\n    return value.strip().lower()\n")
    assert _bool_env_observations(tree, path, prop) == {"bool_literal_set": 0}
    failures = _property_registry_failures((prop,), trees=((path, tree),))
    assert any("zero observations" in failure and "bool_literal_set" in failure for failure in failures), failures


def test_property_registry_rejects_duplicate_owner_count():
    prop = _property("bool-env")
    tree = ast.parse(
        "def parse_bool_value(raw, default, *, env_name):\n"
        "    return default\n"
        "def parse_bool_value(raw, default, *, env_name):\n"
        "    return raw.strip().lower() in {'0', 'false', 'no', 'off'}\n"
    )
    definitions = {
        "parse_bool_value": [
            ("app/validation.py", node)
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "parse_bool_value"
        ]
    }
    assert _property_owner_failures((prop,), definitions) == ["parse_bool_value=2"]


def test_property_registry_rejects_deleted_fingerprint():
    prop = _property("robots-policy")
    weakened = Property(
        name=prop.name,
        owner_module=prop.owner_module,
        owner_symbols=prop.owner_symbols,
        definition_site=prop.definition_site,
        fingerprints=tuple(item for item in prop.fingerprints if item != "robots_url"),
        reason=prop.reason,
        suppressions=prop.suppressions,
    )
    failures = _property_registry_failures((weakened,))
    assert any("robots_url" in failure for failure in failures), failures


def test_property_owner_duplicate_is_not_sanctioned():
    prop = _property("bool-env")
    tree = ast.parse(
        "def parse_bool_value(raw, default, *, env_name):\n"
        "    return default\n"
        "def parse_bool_value(raw, default, *, env_name):\n"
        "    return raw.strip().lower() in {'0', 'false', 'no', 'off'}\n"
    )
    path = REPO_ROOT / "app" / "validation.py"
    owner_ids = _owner_definition_ids(tree, path, prop)
    duplicate = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "parse_bool_value"][1]
    assert len(owner_ids) == 1
    assert not _property_owner(path, prop, tree=tree, node=duplicate)
    assert _bool_env_offenders(tree, path, prop)


def test_public_target_has_one_implementation():
    prop = _property("public-target")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_public_target_offenders(tree, path, prop))
    assert _observed_property_count(prop), "public-target detector observed no fingerprint occurrences"
    assert not offenders, "public-address policy has a second implementation: " + ", ".join(offenders)


def test_public_target_registry_is_not_empty():
    assert _property("public-target").fingerprints


def _synthetic_app_tree(source: str) -> ast.AST:
    """Parse a synthetic module as if it lived at the app-tree root.

    Proof sources are written inline as one string per shape, so a detector
    proof reads as the shape it forbids rather than as harness plumbing.
    """
    return ast.parse(source)


def assert_property_detector_bites(
    prop: Property,
    detector,
    *,
    caught: dict[str, tuple[str | dict[str, str], int]],
    quiet: str | dict[str, str | dict[str, str]],
    quiet_path: pathlib.Path | None = None,
) -> None:
    """Prove a property detector has teeth, in-process, without a break/restore cycle.

    ``caught`` maps a label to (source, exact offender count): every shape must
    be reported with exactly that many offenders, so a detector cannot pass by
    over-reporting. A mapping source places each named module in a separate AST
    and invokes the detector on the final module with all modules available for
    capability resolution. ``quiet`` is a module or labelled modules that
    delegate to the owner and must produce none. This is the entire proof of a
    detector's teeth -- a detector that cannot recognise its own property fails
    here rather than in production.
    """
    path = REPO_ROOT / "app" / "_synthetic.py"

    def run_case(source: str | dict[str, str], default_path: pathlib.Path) -> list[str]:
        if isinstance(source, str):
            return list(detector(_synthetic_app_tree(source), default_path, prop))
        modules = [
            (REPO_ROOT / relative, ast.parse(module_source))
            for relative, module_source in source.items()
        ]
        primary_path, primary_tree = modules[-1]
        return list(detector(primary_tree, primary_path, prop, modules=modules))

    for label, (source, expected) in caught.items():
        offenders = run_case(source, path)
        assert len(offenders) == expected, (
            f"{prop.name} detector: {label} shape produced {len(offenders)} offenders, "
            f"expected {expected}: {offenders}"
        )
    resolved_quiet_path = quiet_path or path
    quiet_sources = quiet if isinstance(quiet, dict) else {"owner delegation": quiet}
    for label, source in quiet_sources.items():
        quiet_offenders = run_case(source, resolved_quiet_path)
        assert not quiet_offenders, f"{prop.name} detector flagged {label}: {quiet_offenders}"


def test_public_target_detector_bites():
    assert_property_detector_bites(
        _property("public-target"),
        _public_target_offenders,
        caught={
            # All four alias forms live in ONE module on purpose: a detector
            # that only inspected a module's first function would pass if each
            # shape were proven in its own module.
            "all alias forms in one module": (
                "import ipaddress\n"
                "import ipaddress as ip\n"
                "from ipaddress import ip_address as parse\n"
                "IP = ipaddress\n"
                "def alias_import(value):\n"
                "    address = ip.ip_address(value)\n"
                "    return address.is_private or ip.ip_network('100.64.0.0/10')\n"
                "def from_import(value):\n"
                "    address = parse(value)\n"
                "    return address.is_private or ip.ip_network('100.64.0.0/10')\n"
                "def assignment(value):\n"
                "    address = IP.ip_address(value)\n"
                "    return address.is_private or IP.ip_network('100.64.0.0/10')\n"
                "def getattr_call(value):\n"
                "    address = getattr(ipaddress, 'ip_address')(value)\n"
                "    return address.is_private or ipaddress.ip_network('100.64.0.0/10')\n",
                4,
            ),
            "helper split with dynamic range access": (
                "import ipaddress\n"
                "def parse_address(value):\n"
                "    return ipaddress.ip_address(value)\n"
                "def verdict(value):\n"
                "    return not getattr(parse_address(value), 'is_private')\n",
                1,
            ),
            "aliased getattr": (
                "from builtins import getattr as ga\n"
                "import ipaddress\n"
                "def verdict(value):\n"
                "    return not ga(ipaddress, 'ip_address')(value).is_private\n",
                1,
            ),
            "constant-folded getattr name": (
                "import ipaddress\n"
                "def verdict(value):\n"
                "    name = 'ip_' + 'address'\n"
                "    return not getattr(ipaddress, name)(value).is_private\n",
                1,
            ),
        },
        quiet=(
            "from app.validation import _is_public_ip as verdict\n"
            "def check(value):\n"
            "    return verdict(value)\n"
        ),
    )
    runtime_name = ast.parse(
        "import ipaddress\n"
        "def attribute_name():\n"
        "    return 'ip_address'\n"
        "def verdict(value):\n"
        "    name = attribute_name()\n"
        "    return not getattr(ipaddress, name)(value).is_private\n"
    )
    assert not _public_target_offenders(
        runtime_name,
        REPO_ROOT / "app" / "_synthetic.py",
        _property("public-target"),
    )


def test_bool_env_has_one_implementation():
    prop = _property("bool-env")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_bool_env_offenders(tree, path, prop))
    assert _observed_property_count(prop), "bool-env detector observed no fingerprint occurrences"
    assert not offenders, "boolean environment policy has a second implementation: " + ", ".join(offenders)


def test_bool_env_registry_is_not_empty():
    assert _property("bool-env").fingerprints


def test_bool_env_detector_bites():
    assert_property_detector_bites(
        _property("bool-env"),
        _bool_env_offenders,
        caught={
            "truthy set": (
                "def parse(value):\n"
                "    return value.strip().lower() in {'1', 'true', 'yes', 'on'}\n",
                1,
            ),
            "inverted set": (
                "def parse(value):\n"
                "    return value.strip().lower() not in {'0', 'false', 'no', 'off'}\n",
                1,
            ),
            "module-level set": (
                "FALSE_WORDS = {'0', 'false', 'no', 'off'}\n"
                "def parse(value):\n"
                "    return value.strip().lower() not in FALSE_WORDS\n",
                1,
            ),
            "helper split through dict keys": (
                "FALSE_WORDS = {'0': False, 'false': False, 'no': False, 'off': False}\n"
                "def normalize(value):\n"
                "    return value.strip().lower()\n"
                "def parse(value):\n"
                "    return normalize(value) in FALSE_WORDS.keys()\n",
                1,
            ),
        },
        quiet=(
            "from app.validation import parse_bool_value as parse\n"
            "def parse_env(value, default):\n"
            "    return parse(value, default, env_name='X')\n"
        ),
    )


def test_robots_policy_has_one_implementation():
    prop = _property("robots-policy")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_robots_policy_offenders(tree, path, prop))
    assert _observed_property_count(prop), "robots-policy detector observed no fingerprint occurrences"
    assert not offenders, "robots policy has a second implementation: " + ", ".join(offenders)


def test_robots_policy_registry_is_not_empty():
    assert _property("robots-policy").fingerprints


def test_robots_policy_detector_bites():
    assert_property_detector_bites(
        _property("robots-policy"),
        _robots_policy_offenders,
        caught={
            "parser call and cache": (
                "from app.bounded_ttl import BoundedTTLCache as Cache\n"
                "from app.crawl.robots_parse import parse_robots_rules as parse\n"
                "def robots_loader(origin):\n"
                "    cache = Cache(max_entries=1, ttl=1)\n"
                "    return parse('User-agent: *')\n",
                2,
            ),
            "parser definition": (
                "def parse_robots_rules(text):\n    return [(True, text)]\n",
                1,
            ),
            "split url literal": (
                "def renamed_loader(origin):\n"
                "    path = '/r' + 'obots.txt'\n"
                "    return http_fetcher(urljoin(origin, path))\n",
                1,
            ),
            "renamed parser definition": (
                "def decode_directives(text):\n"
                "    rows = text.splitlines()\n"
                "    return [(True, row) for row in rows if 'disallow' in row.lower()]\n",
                1,
            ),
            "helper split with formatted url": (
                "def endpoint(origin):\n"
                "    return '{}/robots.txt'.format(origin)\n"
                "def load(origin):\n"
                "    return http_fetcher(endpoint(origin))\n",
                1,
            ),
            "inline joined url": (
                "def load(origin):\n"
                "    return http_fetcher(''.join((origin, '/robots.txt')))\n",
                1,
            ),
            "inline f-string url": (
                "def load(origin):\n"
                "    return http_fetcher(f'{origin}/robots.txt')\n",
                1,
            ),
            "inline absolute formatted url": (
                "def load(origin):\n"
                "    return http_fetcher('https://{}/robots.txt'.format(origin))\n",
                1,
            ),
            "helper split with arbitrary dict parser": (
                "def directives(text):\n"
                "    return {line: True for line in text.splitlines()}\n"
                "def load(text):\n"
                "    return http_fetcher(directives(text))\n",
                1,
            ),
        },
        quiet=(
            "from app.crawl.robots import load_robots_rules as load\n"
            "def check(origin):\n"
            "    return load(origin, fetcher, 5)\n"
        ),
    )


def test_property_detectors_resolve_imported_helpers_across_modules():
    assert_property_detector_bites(
        _property("public-target"),
        _public_target_offenders,
        caught={
            "imported verdict helper": (
                {
                    "app/_public_helper.py": (
                        "import ipaddress\n"
                        "def parse_address(value):\n"
                        "    return ipaddress.ip_address(value)\n"
                    ),
                    "app/_public_consumer.py": (
                        "from app._public_helper import parse_address\n"
                        "def verdict(value):\n"
                        "    address = parse_address(value)\n"
                        "    return address.is_private\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "imported owner": {
                "app/_public_owner.py": "def verdict(value):\n    return value\n",
                "app/_public_consumer.py": (
                    "from app._public_owner import verdict\n"
                    "def check(value):\n"
                    "    return verdict(value)\n"
                ),
            },
        },
    )
    assert_property_detector_bites(
        _property("bool-env"),
        _bool_env_offenders,
        caught={
            "imported normalization helper": (
                {
                    "app/_bool_helper.py": (
                        "def normalize(value):\n"
                        "    return value.strip().lower()\n"
                    ),
                    "app/_bool_consumer.py": (
                        "from app._bool_helper import normalize\n"
                        "FALSE_WORDS = {'0', 'false', 'no', 'off'}\n"
                        "def parse(value):\n"
                        "    return normalize(value) in FALSE_WORDS\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "imported owner": {
                "app/_bool_owner.py": "def parse(value):\n    return value\n",
                "app/_bool_consumer.py": (
                    "from app._bool_owner import parse\n"
                    "def check(value):\n"
                    "    return parse(value)\n"
                ),
            },
        },
    )
    assert_property_detector_bites(
        _property("robots-policy"),
        _robots_policy_offenders,
        caught={
            "imported policy helper": (
                {
                    "app/_robots_helper.py": (
                        "def parse_robot_policy(text):\n"
                        "    return [(True, text)]\n"
                        "def load_policy(text):\n"
                        "    return parse_robot_policy(text)\n"
                    ),
                    "app/_robots_consumer.py": (
                        "from app._robots_helper import load_policy\n"
                        "def load(text):\n"
                        "    return load_policy(text)\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "shared field parser": {
                "app/_robots_primitive.py": (
                    "def parse_fields(text):\n"
                    "    return [(True, line) for line in text.splitlines()]\n"
                ),
                "app/_robots_consumer.py": (
                    "from app._robots_primitive import parse_fields\n"
                    "def load(text):\n"
                    "    return parse_fields(text)\n"
                ),
            },
        },
    )


_KNOB_PATTERN = re.compile(r"\bBROWSER_CRAWLER_[A-Z0-9_]+\b")


def _documented_browser_knobs() -> set[str] | None:
    env_path = REPO_ROOT / ".env.example"
    readme_path = REPO_ROOT / "README.md"
    if not env_path.exists() or not readme_path.exists():
        return None
    env_names = {
        match.group(0)
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
        for match in _KNOB_PATTERN.finditer(line)
    }
    readme_names = set(_KNOB_PATTERN.findall(readme_path.read_text(encoding="utf-8")))
    return {name for name in env_names | readme_names if not name.endswith("_")}


def _settings_alias_declaration_constant(
    path: pathlib.Path,
    tree: ast.AST,
    node: ast.Constant,
) -> bool:
    if _relative_app_path(path) != "app/settings.py":
        return False
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    current: ast.AST = node
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ENVIRONMENT_FIELD_ALIASES"
            for target in current.targets
        ):
            return True
    return False


_ENV_READER_NAMES = frozenset(
    {
        "get",
        "getenv",
        "get_env_float",
        "get_env_int",
        "get_mapping_float",
        "get_mapping_int",
        "parse_bool_env",
        "parse_bool_value",
    }
)


def _environment_reader_reference(
    tree: ast.AST,
    node: ast.Constant,
    field_environments: dict[str, str],
) -> str | None:
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    expression = parents.get(id(node))
    scope = parents.get(id(expression)) if expression is not None else None
    if (
        isinstance(node, ast.Constant)
        and isinstance(expression, ast.Expr)
        and expression.value is node
        and getattr(scope, "body", ())
        and scope.body[0] is expression
    ):
        return None
    current: ast.AST = node
    alias_value: str | None = None
    is_alias_lookup = False
    reaches_reader = False
    while id(current) in parents:
        current = parents[id(current)]
        if (
            isinstance(current, ast.Subscript)
            and isinstance(current.value, ast.Name)
            and current.value.id == "ENVIRONMENT_FIELD_ALIASES"
        ):
            is_alias_lookup = True
            if isinstance(current.slice, ast.Constant) and isinstance(current.slice.value, str):
                alias_value = field_environments.get(current.slice.value)
        if (
            isinstance(current, ast.Call)
            and resolved_call_name(tree, current).rsplit(".", 1)[-1] in _ENV_READER_NAMES
        ):
            reaches_reader = True
    if not reaches_reader:
        return None
    if is_alias_lookup:
        return alias_value
    if node.value.startswith("BROWSER_CRAWLER_"):
        return node.value
    return None


def _settings_fields() -> set[str]:
    tree = file_tree("app/settings.py")[1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CrawlerSettings":
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    return set()


def _settings_alias_failures(
    aliases: dict[str, str],
    fields: set[str] | None = None,
) -> list[str]:
    known_fields = fields if fields is not None else _settings_fields()
    failures = [
        f"{field} maps to invalid environment name {environment}"
        for field, environment in aliases.items()
        if field not in known_fields or not environment.startswith("BROWSER_CRAWLER_")
    ]
    fields_by_environment: dict[str, list[str]] = {}
    for field, environment in aliases.items():
        fields_by_environment.setdefault(environment, []).append(field)
    failures.extend(
        f"{', '.join(sorted(fields_for_environment))} map to {environment}"
        for environment, fields_for_environment in sorted(fields_by_environment.items())
        if len(fields_for_environment) > 1
    )
    return failures


def _production_browser_knob_references() -> set[str]:
    references: set[str] = set()
    fields = _settings_fields()
    field_knobs = {f"BROWSER_CRAWLER_{field.upper()}": field for field in fields}
    for path, tree in app_trees(exclude=frozenset()):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and not _settings_alias_declaration_constant(path, tree, node)
            ):
                environment = _environment_reader_reference(
                    tree,
                    node,
                    ENVIRONMENT_FIELD_ALIASES,
                )
                if environment is not None:
                    references.add(environment)
            if isinstance(node, ast.Attribute) and _relative_app_path(path) != "app/settings.py":
                environment = ENVIRONMENT_FIELD_ALIASES.get(node.attr)
                if environment is not None:
                    references.add(environment)
                conventional = f"BROWSER_CRAWLER_{node.attr.upper()}"
                if conventional in field_knobs:
                    references.add(conventional)
    dockerfile = REPO_ROOT / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith(("CMD", "ENTRYPOINT")):
                references.update(_KNOB_PATTERN.findall(line))
    return references


def _documented_knob_offenders(
    documented: set[str] | None,
    references: set[str],
) -> list[str]:
    if documented is None:
        return ["documented knob source could not be located"]
    if not documented:
        return ["no documented BROWSER_CRAWLER knobs were found"]
    return [f"{name} has no production consumer" for name in sorted(documented - references)]


def _resolved_env_reader_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    references: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            environment = _environment_reader_reference(tree, node, ENVIRONMENT_FIELD_ALIASES)
            if environment is not None:
                references.add(environment)
    return _documented_knob_offenders(
        {"BROWSER_CRAWLER_LAZY_SCROLL_STEPS"},
        references,
    )


def test_documented_browser_knobs_have_consumers():
    assert not _settings_alias_failures(ENVIRONMENT_FIELD_ALIASES)
    failures = _documented_knob_offenders(
        _documented_browser_knobs(),
        _production_browser_knob_references(),
    )
    assert not failures, "; ".join(failures)


def test_documented_knob_detector_bites():
    assert _documented_knob_offenders({"BROWSER_CRAWLER_FAKE"}, set()) == [
        "BROWSER_CRAWLER_FAKE has no production consumer"
    ]
    assert _documented_knob_offenders(None, set()) == ["documented knob source could not be located"]
    current = _documented_browser_knobs()
    assert current is not None
    assert "BROWSER_CRAWLER_ALLOWED_HOSTS" in current
    assert not _documented_knob_offenders({"BROWSER_CRAWLER_ALLOWED_HOSTS"}, {"BROWSER_CRAWLER_ALLOWED_HOSTS"})
    assert _documented_knob_offenders({"BROWSER_CRAWLER_LOG_FILE"}, set())
    assert _settings_alias_failures({"missing_field": "BROWSER_CRAWLER_FAKE"}, {"real_field"})
    assert _settings_alias_failures(
        {
            "browser_lazy_scroll_steps": "BROWSER_CRAWLER_FAKE",
            "log_file_path": "BROWSER_CRAWLER_FAKE",
        },
        {"browser_lazy_scroll_steps", "log_file_path"},
    ) == ["browser_lazy_scroll_steps, log_file_path map to BROWSER_CRAWLER_FAKE"]
    prop = Property(
        name="resolved-env-reader",
        owner_module="app/settings.py",
        owner_symbols=("from_mapping",),
        definition_site="app/settings.py:from_mapping",
        fingerprints=("environment_reader_reference",),
        reason="A documented environment name must resolve through a value reader.",
    )
    assert_property_detector_bites(
        prop,
        _resolved_env_reader_offenders,
        caught={
            "mapping lookup passes field name to reader": (
                "from app.settings import ENVIRONMENT_FIELD_ALIASES\n"
                "def broken(source):\n"
                "    return get_mapping_int(source, ENVIRONMENT_FIELD_ALIASES['BROWSER_CRAWLER_LAZY_SCROLL_STEPS'], 3)\n",
                1,
            ),
            "docstring-only mention": (
                "def read_setting():\n"
                "    '''Mentions BROWSER_CRAWLER_LAZY_SCROLL_STEPS without reading it.'''\n"
                "    return object()\n",
                1,
            ),
        },
        quiet=(
            "from app.settings import ENVIRONMENT_FIELD_ALIASES\n"
            "def resolved(source):\n"
            "    return get_mapping_int(source, ENVIRONMENT_FIELD_ALIASES['browser_lazy_scroll_steps'], 3)\n"
        ),
    )


_MIGRATION_DDL_PROPERTY = Property(
    name="migration-ddl",
    owner_module="app/crawl/migrations/",
    owner_symbols=(),
    definition_site="app/crawl/migrations/",
    fingerprints=("ddl",),
    reason="Schema changes must remain ordered and reversible in the migration package.",
)


def _ddl_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    if _relative_app_path(path).startswith("app/crawl/migrations/"):
        return []
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if re.match(
            r"\s*(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b",
            node.value,
            re.IGNORECASE,
        ):
            found.append(offender(path, node.lineno, node.value.splitlines()[0]))
    return found


def test_ddl_is_confined_to_the_migrations_package():
    offenders = [
        found
        for path, tree in app_trees(exclude=frozenset())
        for found in _ddl_offenders(tree, path, _MIGRATION_DDL_PROPERTY)
    ]
    assert not offenders, "DDL outside app/crawl/migrations: " + ", ".join(offenders)


def test_ddl_detector_bites():
    assert_property_detector_bites(
        _MIGRATION_DDL_PROPERTY,
        _ddl_offenders,
        caught={
            "create alter drop": (
                "def bypass(conn):\n"
                "    conn.execute('CREATE TABLE x (id TEXT)')\n"
                "    conn.execute('ALTER TABLE x ADD COLUMN y TEXT')\n"
                "    conn.execute('DROP TABLE x')\n",
                3,
            )
        },
        quiet="def apply(conn):\n    conn.execute('CREATE TABLE x (id TEXT)')\n",
        quiet_path=REPO_ROOT / "app" / "crawl" / "migrations" / "0001_crawl_jobs_initial.py",
    )
