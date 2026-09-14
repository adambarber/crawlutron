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

from chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    STRATEGIES_DIR,
    TESTS_DIR,
    app_python_files,
    app_trees,
    file_tree,
    offender,
    trees_under,
)

__all__ = [
    "REPO_ROOT",
    "SANCTIONED",
    "STRATEGIES_DIR",
    "app_python_files",
    "app_trees",
    "file_tree",
    "offender",
    "trees_under",
]

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
    assert _lock_bodies_with_awaits(bound, REPO_ROOT / "app" / "_synthetic.py"), "threading-bound name escaped"
    asyncio_ok = ast.parse(
        "import asyncio\n\nclass C:\n    def __init__(self):\n        self._lock = asyncio.Lock()\n"
        "    async def go(self):\n        async with self._lock:\n            await x()\n"
    )
    assert not _lock_bodies_with_awaits(asyncio_ok, REPO_ROOT / "app" / "_synthetic.py"), "asyncio lock flagged"
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
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "UnifiedCrawlResponse"
            and any(kw.arg == "results" for kw in node.keywords)
        ):
            offenders.append(offender(path, node.lineno, "explicit UnifiedCrawlResponse(results=...) build"))
    return offenders


def test_crawl_response_built_only_by_shaping_chokepoint():
    """Every UnifiedCrawlResponse-with-results flows through the shaper."""
    offenders: list[str] = []
    for path, tree in app_trees():
        offenders.extend(_crawl_response_construction_offenders(tree, path))
    assert not offenders, "Use app.api.response_shaping.build_unified_crawl_response: " + ", ".join(offenders)


def test_crawl_response_chokepoint_detector_bites():
    """Synthetic bypass fires; **stored deserialization splat stays clean."""
    bypass = ast.parse("def f(results):\n    return UnifiedCrawlResponse(request_id='r', results=results)\n")
    offenders = _crawl_response_construction_offenders(bypass, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    stored = ast.parse("def f(stored):\n    return UnifiedCrawlResponse(**stored)\n")
    assert not _crawl_response_construction_offenders(stored, REPO_ROOT / "app" / "api" / "_synthetic.py")


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
# Error-envelope shape (jscpd #3 class)
# ---------------------------------------------------------------------------

# app/api/exceptions.py owns the envelope; it is the sanctioned definition
# (exclude matches by file *name*, matching app_trees' own semantics).
ENVELOPE_SANCTIONED = SANCTIONED | {"exceptions.py"}


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _envelope_dict_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag hand-rolled error envelopes; the factory is the only definition.

    Two shapes: a ``HTTPException(detail={...code, message...})`` literal (the
    pre-factory form, with or without ``details``) and any dict carrying all
    three envelope keys anywhere (catches JSONResponse/return-value variants).
    """
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HTTPException":
            for kw in node.keywords:
                if (
                    kw.arg == "detail"
                    and isinstance(kw.value, ast.Dict)
                    and {"code", "message"} <= _dict_keys(kw.value)
                ):
                    offenders.append(offender(path, node.lineno, "HTTPException detail= envelope literal"))
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
    """Both drift shapes fire; factory-shaped code stays clean."""
    bad = ast.parse(
        "raise HTTPException(status_code=404, detail={'code': 'not_found', 'message': 'gone'})\n"
        "response = {'code': 'pending', 'message': 'm', 'details': {}}\n"
    )
    offenders = _envelope_dict_offenders(bad, REPO_ROOT / "app" / "api" / "_synthetic.py")
    assert len(offenders) == 2, offenders
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
