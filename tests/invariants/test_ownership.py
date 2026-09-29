"""State that is acquired must be released by the thing that owns it.

Locks, minted handles, politeness floors, age-based sweeps, semaphores,
pool slots, cookie jars and browser instances are all held across an await
or outlive the call that made them. Each check pins the release to one owner."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    app_offenders,
    app_trees,
    assert_detector,
    assert_fires_once,
    call_nodes,
    calls_named_leaf,
    functions,
    name_reads,
    nodes_of,
    offender,
    offender_at,
    relative_posix,
    subscript_reads,
    unannotated_writes,
    unary_ops,
    with_blocks,
)
from tests.invariants._shared import (
    _enclosing_calls,
    _enclosing_defs,
    assert_trees_clean,
    function_defs,
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
    for node in with_blocks(tree):
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
            for inner in nodes_of(node):
                if isinstance(inner, ast.Await):
                    offenders.append(offender(path, inner.lineno))
            break
    return offenders


_LOCK_NAME_STEMS = ("lock", "mutex", "guard")


_THREADING_LOCK_CONSTRUCTORS = frozenset({"Lock", "RLock", "Condition", "Semaphore", "BoundedSemaphore"})


def _threading_lock_bindings(tree: ast.AST) -> set[str]:
    """Names/attributes bound to threading lock-family constructors."""
    names: set[str] = set()
    for node in nodes_of(tree):
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
    """A lock held across an await blocks the loop and can deadlock."""
    assert_trees_clean(
        _lock_bodies_with_awaits,
        "Lock held across await",
        trees=app_trees(),
    )


def test_lock_detector_bites():
    """Name stems beyond 'lock' and threading-bound aliases must fire."""
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
    for fn in function_defs(tree):
        if fn.name not in _CACHE_TWIN_METHODS:
            continue
        for sub in subscript_reads(fn):
            if isinstance(sub.value, ast.Attribute) and sub.value.attr == "_memory_cache":
                offenders.append(offender_at(path, fn, f"{fn.name}() reads/writes _memory_cache directly"))
                break
    return offenders


def test_memory_cache_only_via_locked_helpers():
    """get/set/aget/aset never touch the raw RAM-mirror dict."""
    assert_trees_clean(
        _memory_cache_twin_offenders,
        "Use _take_memory_locked/_remember_memory_locked",
        trees=app_trees(),
    )


def test_memory_cache_twin_detector_bites():
    """Synthetic drifted twin fires; helper-shaped code stays clean."""
    assert_detector(
        _memory_cache_twin_offenders,
        path=REPO_ROOT / "app" / "crawl" / "_synthetic.py",
        fires={
            "class C:\n    async def aget(self, key):\n        with self._lock:\n"
            "            if key in self._memory_cache:\n"
            "                return self._memory_cache["
            "key][1]\n": "aget() reads/writes _memory_cache directly",
        },
        clean=("class C:\n    async def aget(self, key):\n        return self._take_memory_locked(key)\n",),
    )


# ---------------------------------------------------------------------------
# Lock ownership: a *_locked helper presumes the caller holds the lock
# ---------------------------------------------------------------------------


def _is_locked_helper_name(name: str | None) -> bool:
    # ``name is not None`` rather than ``bool(name)``: both reject the empty
    # string, but only the first states the narrowing the two ``str`` methods
    # below then rely on.
    return name is not None and name.startswith("_") and name.endswith("_locked")


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
    for node in nodes_of(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    offenders: list[str] = []
    for node in call_nodes(tree):
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
            offenders.append(offender_at(path, node, f"{fname}() called outside a lock context manager"))
    return offenders


def test_locked_cache_helpers_are_called_under_the_lock():
    """M1 class: every ``_*_locked`` call sits inside a ``with ...lock`` block."""
    assert_trees_clean(
        _locked_calls_outside_lock_offenders,
        "call *_locked helpers inside `with self._lock`",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_locked_helper_lock_ownership_detector_bites():
    """An off-lock *_locked call fires; the guarded form stays clean."""
    assert_detector(
        _locked_calls_outside_lock_offenders,
        path=REPO_ROOT / "app" / "_synthetic.py",
        fires={
            "class C:\n    def get(self, key):\n        return self._remember_memory_locked(key)\n": (
                "_remember_memory_locked() called outside a lock context manager"
            )
        },
        clean=(
            "class C:\n    def get(self, key):\n        with self._lock:\n"
            "            return self._remember_memory_locked(key)\n",
        ),
    )


# ---------------------------------------------------------------------------
# Handle minting is single-sourced (H4 residue / HIGH #9 entropy policy)
# ---------------------------------------------------------------------------

_ID_MINT_SANCTIONED = frozenset({"ids.py"})


def _id_mint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct ``uuid4()`` calls or ``.hex`` slicing outside app/crawl/ids.

    Handles double as the lookup credential for status/result routes, so the
    full-entropy policy must live in exactly one primitive; a re-inlined mint
    is where a shorter slice silently regresses to 48 bits. The two shapes are
    separate walks because a call and a subscript are different nodes; the
    callee's leaf name is :func:`chokepoint_walks.calls_named_leaf`, shared with
    the error-envelope sweep, so "a banned chokepoint reached as ``mod.name()``
    or as a bare import" is decided once.
    """
    return [
        offender_at(path, call, "uuid4() minted outside app/crawl/ids.py")
        for call in calls_named_leaf(tree, {"uuid4"})
    ] + [
        offender_at(path, node, "uuid hex sliced outside app/crawl/ids.py")
        for node in subscript_reads(tree)
        if getattr(node.value, "attr", None) == "hex"
    ]


def test_handles_are_minted_in_one_place():
    """Only app/crawl/ids.py calls uuid4 or slices its hex (H4 residue)."""
    assert_trees_clean(
        _id_mint_offenders,
        "handle minting is single-sourced in app/crawl/ids.py",
        trees=app_trees(exclude=SANCTIONED | _ID_MINT_SANCTIONED),
    )


def test_id_mint_detector_bites():
    assert_detector(
        _id_mint_offenders,
        path=REPO_ROOT / "app" / "api" / "_synthetic.py",
        # uuid4 call AND hex slice are two findings, not one.
        trips={"import uuid\n\ndef f():\n    return uuid.uuid4().hex[:12]\n": 2},
        clean=("from app.crawl.ids import new_request_id\n\ndef f():\n    return new_request_id()\n",),
    )


# ---------------------------------------------------------------------------
# Robots politeness floor is committed only through the bounded setter
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
        node for node in nodes_of(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if func.name == "set_domain_min_delay":
            continue
        for node in unannotated_writes(func):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Attribute)
                    and target.value.attr == "_domain_min_delay"
                ):
                    offenders.append(
                        offender_at(path, node, f"unbounded write to _domain_min_delay in {func.name}()")
                    )
    return offenders


def test_domain_politeness_floor_is_committed_only_via_the_setter():
    """Only set_domain_min_delay writes _domain_min_delay (M1 chokepoint)."""
    assert_trees_clean(
        _unbounded_domain_delay_writes,
        "commit the Crawl-delay floor through the clamped set_domain_min_delay",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_domain_delay_detector_bites():
    assert_detector(
        _unbounded_domain_delay_writes,
        path=REPO_ROOT / "app" / "_synthetic.py",
        # Both a write and an augmented assignment are unbounded; only the
        # setter's clamped write is not.
        trips={
            "class S:\n    def other(self):\n        self._domain_min_delay['x'] = 1e9\n"
            "    def bump(self):\n        self._domain_min_delay['y'] += 1\n": 2,
        },
        clean=(
            "class S:\n    def set_domain_min_delay(self, d, s):\n"
            "        self._domain_min_delay[d] = min(s, 300.0)\n"
            "    def read(self, d):\n        return self._domain_min_delay.get(d, 0.0)\n",
        ),
    )


# ---------------------------------------------------------------------------
# Age-based deletion must consult ownership, not just age
# ---------------------------------------------------------------------------
# A sweep, GC, evict, prune or clean function that unlinks a file is deleting
# something a concurrent worker may still be reading. Age is not ownership: a
# scratch file lives exactly as long as the work writing it, and a process-wide
# sweep reaches every one of them. `_ResultSpill` learned this the hard way --
# its sweep unlinked by age alone and documented the loss of a still-active
# spill as acceptable, when the spill being deleted was the crawl's own
# reassembled result. Deletion by age is fine; deletion by age alone is not.
_SWEEP_NAME_PARTS = ("gc", "sweep", "evict", "prune", "clean", "reap", "purge")


_OWNERSHIP_GUARDS = ("in_use", "live", "in_flight", "active", "owned", "holder", "registry")


def _unowned_age_deletions(tree, path) -> list[str]:
    """Report sweep-like functions that unlink without consulting ownership."""
    offenders: list[str] = []
    for func in functions(tree):
        name = func.name.lower()
        if not any(part in name for part in _SWEEP_NAME_PARTS):
            continue
        unlinks = [
            node
            for node in ast.walk(func)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("unlink", "remove", "rmdir")
        ]
        if not unlinks:
            continue
        body_names = {node.id for node in ast.walk(func) if isinstance(node, ast.Name)}
        body_names |= {node.attr for node in ast.walk(func) if isinstance(node, ast.Attribute)}
        if any(guard in candidate.lower() for candidate in body_names for guard in _OWNERSHIP_GUARDS):
            continue
        offenders.append(
            f"{path.name}:{func.lineno} {func.name}() unlinks without an ownership check; "
            f"skip paths that are live (age alone is not ownership)"
        )
    return offenders


def test_age_based_deletion_consults_ownership_first():
    offenders = app_offenders(_unowned_age_deletions)
    assert not offenders, "; ".join(offenders)


def test_unowned_age_deletion_detector_bites():
    # The shape that lost a live crawl result: a sweep that unlinks by age only.
    bad = ast.parse(
        "def _gc_stale(max_age):\n"
        "    for name in os.listdir(tmp):\n"
        "        if now - os.path.getmtime(name) > max_age:\n"
        "            os.unlink(name)\n"
    )
    assert len(_unowned_age_deletions(bad, REPO_ROOT / "app" / "_synthetic.py")) == 1

    # The fixed shape: the same sweep, asking who owns each candidate first.
    # The guard has to be something the KERNEL can answer -- ownership another
    # process can also see -- not a set this process happens to be holding.
    good = ast.parse(
        "def _gc_stale(max_age):\n"
        "    for name in os.listdir(tmp):\n"
        "        if not _spill_is_unowned(name):\n"
        "            continue\n"
        "        os.unlink(name)\n"
    )
    assert not _unowned_age_deletions(good, REPO_ROOT / "app" / "_synthetic.py")

    # A function that deletes but is not a sweep is not this gate's business.
    not_a_sweep = ast.parse("def close(self):\n    os.unlink(self.path)\n")
    assert not _unowned_age_deletions(not_a_sweep, REPO_ROOT / "app" / "_synthetic.py")

    # A method named for cleanup still has to ask.
    method = ast.parse("class C:\n    def cleanup(self):\n        os.remove(self.path)\n")
    assert len(_unowned_age_deletions(method, REPO_ROOT / "app" / "_synthetic.py")) == 1


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
    return [
        offender_at(
            path,
            node,
            f"{node.id} touched outside {_SEMAPHORE_ACCESSOR}; "
            f"use the accessor so the instance stays bound to the running loop",
        )
        for node in name_reads(tree)
        if node.id in _SEMAPHORE_GLOBALS
        and (scopes := _enclosing_defs(node, tree))
        and _SEMAPHORE_ACCESSOR not in scopes
    ]


def test_async_crawl_semaphore_globals_are_loop_bound():
    assert not (offenders := app_offenders(_semaphore_global_touch_offenders)), ", ".join(offenders)


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
    return any(isinstance(sub, ast.Attribute) and sub.attr == "retired" for sub in nodes_of(node))


def _reaps_retired(value: ast.AST) -> bool:
    """A ``_DOC_POOL = [... for each ... if not each.retired]`` style rebuild.

    ``not each.retired`` parses as a ``UnaryOp(Not, ...)`` and an explicit
    comparison as a ``Compare``; both shapes are admitted.
    """
    if not isinstance(value, ast.ListComp):
        return False
    for generator in value.generators:
        for condition in generator.ifs:
            for node in unary_ops(condition):
                if isinstance(node.op, ast.Not):
                    if _has_retired_attr(node.operand):
                        return True
                if isinstance(node, ast.Compare):
                    if any(_has_retired_attr(operand) for operand in (node.left, *node.comparators)):
                        return True
    return False


def _rebuilds_pool_without_retired(node: ast.AST) -> bool:
    """Does ``node`` assign ``_DOC_POOL`` from a list comp filtering ``retired``?"""
    return any(
        isinstance(child, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == _DOC_POOL_NAME for target in child.targets)
        and _reaps_retired(child.value)
        for child in nodes_of(node)
    )


def _doc_pool_reap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """``_acquire_doc_worker`` must rebuild the pool without retired slots.

    P2-D: a retired slot has been terminated and can never be handed out, so it
    must not keep occupying a pool entry. The respawn scan only replaces a slot
    whose process is dead, which leaves a retired-but-still-``is_alive()`` slot
    skipped by both scans and stranded in the pool forever -- the entry count
    drifted permanently above the usable count. Removing the reap filter is the
    regression; this detector fails the suite when it goes.
    """
    return [
        offender_at(
            path,
            node,
            f"{_DOC_POOL_ACQUIRER} no longer reaps retired slots; a "
            f"retired-but-alive slot would strand in the pool and inflate the size budget",
        )
        for node in functions(tree)
        if node.name == _DOC_POOL_ACQUIRER and not _rebuilds_pool_without_retired(node)
    ]


def test_doc_pool_acquirer_reaps_retired_slots():
    assert not (offenders := app_offenders(_doc_pool_reap_offenders)), ", ".join(offenders)


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


# --- Response cookies enter the jar only through the fetch-completion tail -----
#
# The httpx and curl_cffi fetchers each carried their own copy of the tail that
# decompresses, normalises headers and persists cookies, and the copies had
# already drifted: httpx normalised the response headers into a dict and handed
# *that* to the cookie helper, which is what knocked it off its documented
# get_list path, while curl_cffi handed over the transport's own multidict.
# Collapsing both into fetchers._finish_fetch is what removed the drift. These
# two rules are the enforcement half: the jar has exactly one write door, and
# that door is the shared tail. A third fetch path growing its own cookie
# handling trips them at the line, not after a session is silently missing a
# cookie.

_COOKIE_TAIL = "app/crawl/strategies/fetchers.py"


_COOKIE_HELPER = "app/crawl/strategies/request_profile.py"


_COOKIE_TAIL_FN = "_finish_fetch"


_COOKIE_HELPER_FN = "_store_set_cookies"


def _cookie_writes_off_the_fetch_tail(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a cookie write that does not run through the shared fetch tail."""
    relative = relative_posix(path)
    offenders: list[str] = []
    for call, enclosing in _enclosing_calls(tree, "_store_set_cookies"):
        if relative == _COOKIE_TAIL and enclosing == _COOKIE_TAIL_FN:
            continue
        offenders.append(offender_at(path, call, f"_store_set_cookies() outside {_COOKIE_TAIL_FN}()"))
    for call, enclosing in _enclosing_calls(tree, "add_cookie_header"):
        if relative == _COOKIE_HELPER and enclosing == _COOKIE_HELPER_FN:
            continue
        offenders.append(offender_at(path, call, f"add_cookie_header() outside {_COOKIE_HELPER_FN}()"))
    return offenders


def test_response_cookies_enter_the_jar_only_through_the_fetch_tail():
    """Cookie persistence has one door, and that door is the shared fetch tail."""
    offenders = app_offenders(_cookie_writes_off_the_fetch_tail)
    assert not offenders, (
        f"Persist response cookies via {_COOKIE_TAIL}::{_COOKIE_TAIL_FN}, which calls "
        f"{_COOKIE_HELPER_FN} for you, instead of reaching for the jar where you are: " + ", ".join(offenders)
    )


def test_cookie_tail_detector_bites():
    """Every bypass shape fires; the sanctioned two-hop chain stays clean."""
    synthetic = REPO_ROOT / "app" / "_synthetic.py"
    assert_fires_once(
        _cookie_writes_off_the_fetch_tail,
        {
            "import x\nx._store_set_cookies(url, headers)\n": "_store_set_cookies()",
            "import jar\njar.add_cookie_header(url, cookie)\n": "add_cookie_header()",
            "def nested():\n    jar.add_cookie_header(url, cookie)\n": "add_cookie_header()",
        },
        synthetic,
    )

    tail_call = (
        "from app.crawl.strategies.request_profile import _store_set_cookies\n"
        "def _finish_fetch(**kwargs):\n"
        "    return _store_set_cookies(kwargs)\n"
    )
    # The sanctioned chain is silent at its own path...
    assert not _cookie_writes_off_the_fetch_tail(ast.parse(tail_call), REPO_ROOT / _COOKIE_TAIL)
    assert not _cookie_writes_off_the_fetch_tail(
        ast.parse("def _store_set_cookies(url, headers):\n    jar.add_cookie_header(url, c)\n"),
        REPO_ROOT / _COOKIE_HELPER,
    )
    # ...and the very same source still fires anywhere else. A detector keyed on
    # the function name alone would wave this tree through, which is the whole
    # reason the rule is keyed on the path.
    assert _cookie_writes_off_the_fetch_tail(ast.parse(tail_call), synthetic)


# --- browser instance release -------------------------------------------------
# Both the per-attempt reset and the service-shutdown close have to forget the
# same cached fields. Spelling that out twice is how one of them grows a stale
# field the other never clears -- which is exactly what happened to
# ``_browser_reused`` before _release_browser existed. Only the owner may
# clear them, so a new field cannot be added to one path and forgotten on the
# other.
_BROWSER_RELEASE_FIELDS = ("_browser_cm", "_browser", "_browser_reused")


_BROWSER_RELEASE_OWNER = "app/crawl/strategies/browser.py"


_BROWSER_RELEASE_CHOKEPOINT = "_release_browser"


#: __init__ puts the fields at their initial None; that is not a release.
_BROWSER_RELEASE_EXEMPT = frozenset({"__init__"})


def _browser_release_offenders(tree, path, *, owner, fields, chokepoint):
    # Path == Path, not path.name: comparing a str against a Path is never
    # equal, which would make the whole sweep silently yield nothing.
    if path != owner:
        return
    for node in functions(tree):
        if node.name in (chokepoint, *_BROWSER_RELEASE_EXEMPT):
            continue
        for inner in ast.walk(node):
            # Only clearing to None is a release. _acquire_browser assigns a
            # real context manager and a real browser, which is the opposite
            # of forgetting them.
            cleared = (
                [
                    t
                    for t in inner.targets
                    if isinstance(t, ast.Attribute)
                    and t.attr in fields
                    and isinstance(inner.value, ast.Constant)
                    and inner.value.value is None
                ]
                if isinstance(inner, ast.Assign)
                else []
            )
            for target in cleared:
                yield offender_at(
                    path,
                    inner,
                    f"{target.attr} is cleared to None in {node.name}() rather than {chokepoint}(); route "
                    f"the release through the chokepoint so every exit path clears the same set of fields",
                )


def test_browser_instance_fields_are_cleared_only_by_the_release_chokepoint():
    offenders: list[str] = []
    for path, tree in app_trees(exclude=SANCTIONED):
        offenders.extend(
            _browser_release_offenders(
                tree,
                path,
                owner=REPO_ROOT / _BROWSER_RELEASE_OWNER,
                fields=_BROWSER_RELEASE_FIELDS,
                chokepoint=_BROWSER_RELEASE_CHOKEPOINT,
            )
        )
    assert not offenders, "the browser release chokepoint was bypassed: " + ", ".join(offenders)


def test_browser_release_detector_bites():
    # The reset is generated from the field tuple the detector sweeps, so the
    # synthetic source cannot claim to cover a field the guard does not check.
    # Written out twice it was the same three lines of drift-prone fixture, and
    # the drift is invisible: a field added to the tuple would leave this proof
    # asserting on a source that never mentions it.
    reset = "\n".join(f"        self.{field} = None" for field in _BROWSER_RELEASE_FIELDS)
    source = f"""
class Renderer:
    def __init__(self):
{reset}

    async def {_BROWSER_RELEASE_CHOKEPOINT}(self):
{reset}

    async def close(self):
        self._browser = None
"""
    args = {
        "owner": REPO_ROOT / _BROWSER_RELEASE_OWNER,
        "fields": _BROWSER_RELEASE_FIELDS,
        "chokepoint": _BROWSER_RELEASE_CHOKEPOINT,
    }
    # list(), not the generator: a generator object is always truthy, so
    # asserting on one directly would pass without ever running the sweep.
    offenders = list(
        _browser_release_offenders(ast.parse(source), REPO_ROOT / _BROWSER_RELEASE_OWNER, **args)
    )
    assert offenders, "a field cleared outside the release chokepoint escaped the sweep"
    elsewhere = list(
        _browser_release_offenders(
            ast.parse(source), REPO_ROOT / "app" / "crawl" / "content_cache.py", **args
        )
    )
    assert not elsewhere, f"scoped to the wrong module -- ignored: {elsewhere}"
    clean = "class R:\n    async def _release_browser(self):\n        self._browser = None\n"
    assert not list(
        _browser_release_offenders(ast.parse(clean), REPO_ROOT / _BROWSER_RELEASE_OWNER, **args)
    ), "the chokepoint's own body must not be flagged"
