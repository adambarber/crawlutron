"""State that is acquired must be released by the thing that owns it.

Locks, minted handles, politeness floors, age-based sweeps, semaphores,
pool slots, cookie jars and browser instances are all held across an await
or outlive the call that made them. Each check pins the release to one owner."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    assignment_targets,
    call_nodes,
    calls_named_leaf,
    class_defs,
    functions,
    name_reads,
    nodes_of,
    offender_at,
    relative_posix,
    resolved_call_name,
    subscript_reads,
    unannotated_writes,
    unary_ops,
    with_blocks,
    writes,
)
from tests.invariants._shared import _enclosing_calls, _enclosing_defs, function_defs
from tests.rules import Bites, Rule, Silent, Trips


def _lock_bodies_with_awaits(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag `with <lock>:` bodies containing an await (the ).

    A THREADING lock held across an await blocks the event loop and can
    deadlock. `async with` (asyncio.Lock/Condition) bodies may legitimately
    await -- that is the sanctioned pattern -- so only sync `with` is
    checked. Sync-with bodies that await include any `threading.Lock`/
    `RLock` context (which is what ContentCache.aset did before
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
                    offenders.append(
                        offender_at(
                            path,
                            inner,
                            f"await inside `with {referred}` holds a threading lock across the "
                            f"await, which blocks the event loop; take the async wait outside the "
                            f"`with` and hold only the lock inside it",
                        )
                    )
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


_OWNER_PATH = REPO_ROOT / "app" / "transport" / "browser.py"
_OWNER_MODULE = "app/transport/browser.py"
_ELSEWHERE = REPO_ROOT / "app" / "crawl" / "content_cache.py"


LOCK_HELD_ACROSS_AN_AWAIT = Rule(
    name="lock-held-across-an-await",
    find=_lock_bodies_with_awaits,
    summary=(
        "A threading lock held across an await blocks the event loop; route the "
        "asynchronous wait outside the `with` and take only the lock inside it"
    ),
    bite=lambda: (
        # A name stem, and a name bound to a threading lock-family constructor: the
        # second is the one a rename tries to escape, since self._mutex is not a
        # lock by name at all.
        Bites(
            "class C:\n    def __init__(self):\n        self._mutex = __import__('threading').Lock()\n"
            "    async def go(self):\n        with self._mutex:\n            await x()\n",
            "holds a threading lock across the",
        ),
        Bites(
            "import threading\n\n_async_gate = threading.RLock()\n\n"
            "async def go():\n    with _async_gate:\n        await x()\n",
            "holds a threading lock across the",
        ),
    ),
    clean=lambda: (
        # asyncio.Lock bodies may await -- that IS the sanctioned pattern, and the
        # only reason the sweep reads `with` and skips `async with`.
        "import asyncio\n\nclass C:\n    def __init__(self):\n        self._lock = asyncio.Lock()\n"
        "    async def go(self):\n        async with self._lock:\n            await x()\n",
        # A context manager that is not a lock is not this rule's business.
        "def go():\n    with open('f'):\n        pass\n",
    ),
)


def test_no_await_inside_lock_bodies():
    """A lock held across an await blocks the loop and can deadlock."""
    LOCK_HELD_ACROSS_AN_AWAIT.scan()


def test_lock_detector_bites():
    """Name stems beyond 'lock' and threading-bound aliases must fire."""
    LOCK_HELD_ACROSS_AN_AWAIT.check_bites()


# ---------------------------------------------------------------------------
# ContentCache mirror twins
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


MEMORY_CACHE_TWINS_MUST_GO_THROUGH_THE_LOCKED_HELPERS = Rule(
    name="memory-cache-twins-must-go-through-the-locked-helpers",
    find=_memory_cache_twin_offenders,
    summary=(
        "aget once lost the LRU count cap and the pre-write mirror never rolled "
        "back, both invisible to a twin review; probe and store through "
        "_take_memory_locked/_remember_memory_locked so the twins cannot drift"
    ),
    bite=lambda: Bites(
        "class C:\n    async def aget(self, key):\n        with self._lock:\n"
        "            if key in self._memory_cache:\n"
        "                return self._memory_cache["
        "key][1]\n",
        "aget() reads/writes _memory_cache directly",
    ),
    clean=lambda: (
        "class C:\n    async def aget(self, key):\n        return self._take_memory_locked(key)\n",
    ),
)


def test_memory_cache_only_via_locked_helpers():
    """Get/set/aget/aset never touch the raw RAM-mirror dict."""
    MEMORY_CACHE_TWINS_MUST_GO_THROUGH_THE_LOCKED_HELPERS.scan()


def test_memory_cache_twin_detector_bites():
    """Synthetic drifted twin fires; helper-shaped code stays clean."""
    MEMORY_CACHE_TWINS_MUST_GO_THROUGH_THE_LOCKED_HELPERS.check_bites()


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


LOCKED_HELPERS_ARE_CALLED_UNDER_THE_LOCK = Rule(
    name="locked-helpers-are-called-under-the-lock",
    find=_locked_calls_outside_lock_offenders,
    summary=(
        "A *_locked helper touches shared index/RAM state with no internal "
        "acquisition; call it inside `with self._lock` or the async and sync "
        "paths drift apart again"
    ),
    bite=lambda: Bites(
        "class C:\n    def get(self, key):\n        return self._remember_memory_locked(key)\n",
        "called outside a lock context manager",
    ),
    clean=lambda: (
        "class C:\n    def get(self, key):\n        with self._lock:\n"
        "            return self._remember_memory_locked(key)\n",
    ),
)


def test_locked_cache_helpers_are_called_under_the_lock():
    """Every ``_*_locked`` call sits inside a ``with...lock`` block."""
    LOCKED_HELPERS_ARE_CALLED_UNDER_THE_LOCK.scan()


def test_locked_helper_lock_ownership_detector_bites():
    """An off-lock *_locked call fires; the guarded form stays clean."""
    LOCKED_HELPERS_ARE_CALLED_UNDER_THE_LOCK.check_bites()


# ---------------------------------------------------------------------------
# Handle minting is single-sourced (entropy policy)
# ---------------------------------------------------------------------------


def _id_mint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct ``uuid4()`` calls or ``.hex`` slicing outside app/storage/ids.

    Handles double as the lookup credential for status/result routes, so the
    full-entropy policy must live in exactly one primitive; a re-inlined mint
    is where a shorter slice silently regresses to 48 bits. The two shapes are
    separate walks because a call and a subscript are different nodes; the
    callee's leaf name is :func:`chokepoint_walks.calls_named_leaf`, shared with
    the error-envelope sweep, so "a banned chokepoint reached as ``mod.name()``
    or as a bare import" is decided once.
    """
    return [
        offender_at(path, call, "uuid4() minted outside app/storage/ids.py")
        for call in calls_named_leaf(tree, {"uuid4"})
    ] + [
        offender_at(path, node, "uuid hex sliced outside app/storage/ids.py")
        for node in subscript_reads(tree)
        if getattr(node.value, "attr", None) == "hex"
    ]


HANDLE_MINTING_IS_SINGLE_SOURCED = Rule(
    name="handle-minting-is-single-sourced",
    find=_id_mint_offenders,
    summary=(
        "Handles double as the lookup credential for the status/result routes, "
        "so a re-inlined mint is where a shorter slice silently regresses to 48 "
        "bits; mint through app/storage/ids.py"
    ),
    skip=frozenset({"app/storage/ids.py"}),
    bite=lambda: Trips(
        "import uuid\n\ndef f():\n    return uuid.uuid4().hex[:12]\n",
        count=2,
    ),
    clean=lambda: ("from app.storage.ids import new_request_id\n\ndef f():\n    return new_request_id()\n",),
)


def test_handles_are_minted_in_one_place():
    """Only app/storage/ids.py calls uuid4 or slices its hex ()."""
    HANDLE_MINTING_IS_SINGLE_SOURCED.scan()


def test_id_mint_detector_bites():
    HANDLE_MINTING_IS_SINGLE_SOURCED.check_bites()


# ---------------------------------------------------------------------------
# Robots politeness floor is committed only through the bounded setter
# ---------------------------------------------------------------------------


def _unbounded_domain_delay_writes(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct writes to ``_domain_min_delay`` outside ``set_domain_min_delay``.

    The robots Crawl-delay floor is clamped to a finite politeness ceiling only
    in ``set_domain_min_delay``; a bare ``self._domain_min_delay[...] = ...``
    anywhere else (even ``+=``) bypasses that bound and can drop an
    ``inf``/huge value straight into ``asyncio.sleep``.
    """
    offenders: list[str] = []
    for func in (
        node for node in nodes_of(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if func.name == "set_domain_min_delay":
            continue
        for node in unannotated_writes(func):
            targets = assignment_targets(node)
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


DOMAIN_POLITENESS_FLOOR_ONLY_VIA_THE_SETTER = Rule(
    name="domain-politeness-floor-only-via-the-setter",
    find=_unbounded_domain_delay_writes,
    summary=(
        "The Crawl-delay floor is clamped to a finite politeness ceiling only in "
        "set_domain_min_delay, so any other write can drop an inf straight into "
        "asyncio.sleep; commit it through the clamped setter"
    ),
    bite=lambda: Trips(
        "class S:\n    def other(self):\n        self._domain_min_delay['x'] = 1e9\n"
        "    def bump(self):\n        self._domain_min_delay['y'] += 1\n",
        count=2,
    ),
    clean=lambda: (
        # The setter's clamped write and a read are both fine: the rule is about
        # who may WRITE, and an augmented assignment is still a write.
        "class S:\n    def set_domain_min_delay(self, d, s):\n"
        "        self._domain_min_delay[d] = min(s, 300.0)\n"
        "    def read(self, d):\n        return self._domain_min_delay.get(d, 0.0)\n",
    ),
)


def test_domain_politeness_floor_is_committed_only_via_the_setter():
    """Only set_domain_min_delay writes _domain_min_delay (chokepoint)."""
    DOMAIN_POLITENESS_FLOOR_ONLY_VIA_THE_SETTER.scan()


def test_domain_delay_detector_bites():
    DOMAIN_POLITENESS_FLOOR_ONLY_VIA_THE_SETTER.check_bites()


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


AGE_BASED_DELETION_CONSULTS_OWNERSHIP = Rule(
    name="age-based-deletion-consults-ownership",
    find=_unowned_age_deletions,
    summary=(
        "Age is not ownership: a scratch file lives exactly as long as the work "
        "writing it, and a process-wide sweep reaches every one of them. Skip "
        "paths that are live rather than unlinking by age alone."
    ),
    bite=lambda: (
        Bites(
            "def _gc_stale(max_age):\n"
            "    for name in os.listdir(tmp):\n"
            "        if now - os.path.getmtime(name) > max_age:\n"
            "            os.unlink(name)\n",
            "unlinks without an ownership check",
        ),
        # A method named for cleanup still has to ask; the name is a trigger, not
        # a grant.
        Bites(
            "class C:\n    def cleanup(self):\n        os.remove(self.path)\n",
            "unlinks without an ownership check",
        ),
    ),
    clean=lambda: (
        # The guard has to be something the KERNEL can answer -- ownership another
        # process can also see -- not a set this process happens to be holding.
        "def _gc_stale(max_age):\n"
        "    for name in os.listdir(tmp):\n"
        "        if not _spill_is_unowned(name):\n"
        "            continue\n"
        "        os.unlink(name)\n",
        # A function that deletes but is not a sweep is not this rule's business.
        "def close(self):\n    os.unlink(self.path)\n",
    ),
)


def test_age_based_deletion_consults_ownership_first():
    AGE_BASED_DELETION_CONSULTS_OWNERSHIP.scan()


def test_unowned_age_deletion_detector_bites():
    # The shape that lost a live crawl result: a sweep that unlinks by age only.
    AGE_BASED_DELETION_CONSULTS_OWNERSHIP.check_bites()


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


SEMAPHORE_GLOBALS_ARE_LOOP_BOUND = Rule(
    name="semaphore-globals-are-loop-bound",
    find=_semaphore_global_touch_offenders,
    summary=(
        "The cached Semaphore is bound to the loop that built it, so reading or "
        "assigning the global anywhere but the accessor is a Semaphore awaited "
        "off its own loop; go through the accessor instead"
    ),
    bite=lambda: (
        Bites(
            "async def f():\n    return _async_crawl_semaphore.acquire()\n",
            "use the accessor so the instance stays bound to the running loop",
        ),
        # A direct write is the same bypass.
        Bites(
            "def f():\n    _async_crawl_semaphore = None\n",
            "use the accessor so the instance stays bound to the running loop",
        ),
    ),
    clean=lambda: (
        # The module-level declaration is the definition, not a touch.
        "_async_crawl_semaphore: asyncio.Semaphore | None = None\n",
        # The accessor itself is exempt, including the companion loop global.
        "def _get_async_crawl_semaphore():\n"
        "    global _async_crawl_semaphore, _async_crawl_semaphore_loop\n"
        "    if _async_crawl_semaphore is None:\n"
        "        _async_crawl_semaphore = _build_async_crawl_semaphore()\n"
        "    return _async_crawl_semaphore\n",
    ),
)


def test_async_crawl_semaphore_globals_are_loop_bound():
    SEMAPHORE_GLOBALS_ARE_LOOP_BOUND.scan()


def test_async_crawl_semaphore_loop_binding_detector_bites():
    SEMAPHORE_GLOBALS_ARE_LOOP_BOUND.check_bites()


# --- 4d: the doc pool reaps retired slots on every acquisition ---------------

# Scoped to the class that owns doc workers, not to the bare method name:
# ``acquire`` is a common name for taking a resource out of a pool, and three
# unrelated modules define one that has no retired slots to reap.
_DOC_POOL_CLASS = "DocExtractionPool"

_DOC_POOL_ACQUIRER = "acquire"


def _has_retired_attr(node: ast.AST) -> bool:
    return any(isinstance(sub, ast.Attribute) and sub.attr == "retired" for sub in nodes_of(node))


def _reaps_retired(value: ast.AST) -> bool:
    """A ``self._slots = [... for each... if not each.retired]`` style rebuild.

    ``not each.retired`` parses as a ``UnaryOp(Not,...)`` and an explicit
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
    """Does ``node`` assign ``self._slots`` from a list comp filtering ``retired``?"""
    return any(
        isinstance(child, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == "_slots" for target in child.targets)
        and _reaps_retired(child.value)
        for child in nodes_of(node)
    )


def _doc_pool_reap_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``acquire`` that never rebuilds the pool without retired slots."""
    return [
        offender_at(
            path,
            node,
            f"{_DOC_POOL_CLASS}.{_DOC_POOL_ACQUIRER} no longer reaps retired slots; a "
            f"retired-but-alive slot would strand in the pool and inflate the size budget",
        )
        for cls in (each for each in nodes_of(tree) if isinstance(each, ast.ClassDef))
        if cls.name == _DOC_POOL_CLASS
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
        and node.name == _DOC_POOL_ACQUIRER
        and not _rebuilds_pool_without_retired(node)
    ]


DOC_POOL_ACQUIRER_REAPS_RETIRED_SLOTS = Rule(
    name="doc-pool-acquirer-reaps-retired-slots",
    find=_doc_pool_reap_offenders,
    summary=(
        "A retired slot can never be handed out, so leaving one in the pool "
        "strands it forever and inflates the pool-size budget; rebuild the pool "
        "without retired entries in DocExtractionPool.acquire"
    ),
    bite=lambda: (
        Bites(
            "class DocExtractionPool:\n"
            "    def acquire(self):\n"
            "        with self._lock:\n"
            "            want = self._worker_count()\n"
            "            while True:\n"
            "                for worker in self._slots:\n"
            "                    if not worker.busy:\n"
            "                        return worker\n",
            "no longer reaps retired slots",
        ),
        Bites(
            "class DocExtractionPool:\n"
            "    def acquire(self):\n"
            "        with self._lock:\n"
            "            self._slots = [each for each in self._slots if not each.busy]\n"
            "            return self._slots[0]\n",
            "no longer reaps retired slots",
        ),
    ),
    clean=lambda: (
        "class DocExtractionPool:\n"
        "    def acquire(self):\n"
        "        with self._lock:\n"
        "            self._slots = [each for each in self._slots if not each.retired]\n"
        "            return self._slots[0]\n",
    ),
)


def test_doc_pool_acquirer_reaps_retired_slots():
    DOC_POOL_ACQUIRER_REAPS_RETIRED_SLOTS.scan()


def test_doc_pool_reap_detector_bites():
    DOC_POOL_ACQUIRER_REAPS_RETIRED_SLOTS.check_bites()


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

_COOKIE_TAIL = "app/transport/fetchers.py"


_COOKIE_HELPER = "app/transport/request_profile.py"


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


RESPONSE_COOKIES_ENTER_VIA_THE_FETCH_TAIL = Rule(
    name="response-cookies-enter-via-the-fetch-tail",
    find=_cookie_writes_off_the_fetch_tail,
    summary=(
        "The httpx and curl_cffi fetchers each carried their own cookie tail and "
        "had already drifted, so a third fetch path growing its own handling is a "
        "silently missing cookie; persist through fetchers._finish_fetch"
    ),
    bite=lambda: (
        Bites(
            "import x\nx._store_set_cookies(url, headers)\n",
            "_store_set_cookies() outside _finish_fetch()",
        ),
        Bites(
            "import jar\njar.add_cookie_header(url, cookie)\n",
            "add_cookie_header() outside _store_set_cookies()",
        ),
        Bites("def nested():\n    jar.add_cookie_header(url, cookie)\n", "add_cookie_header() outside"),
    ),
    clean=lambda: (
        # The sanctioned two-hop chain, spared at the paths that own it. Both
        # halves are Silents rather than lookalike strings: each is sanctioned at
        # ONE path and must fire at every other, so a string -- which is always run
        # at the rule's single proof path -- would assert the opposite claim here.
        Silent(
            "from app.transport.request_profile import _store_set_cookies\n"
            "def _finish_fetch(**kwargs):\n"
            "    return _store_set_cookies(kwargs)\n",
            at=REPO_ROOT / _COOKIE_TAIL,
        ),
        Silent(
            "def _store_set_cookies(url, headers):\n    jar.add_cookie_header(url, c)\n",
            at=REPO_ROOT / _COOKIE_HELPER,
        ),
        # The half that makes it a PATH-keyed rule: that very same fetch-tail
        # source still fires anywhere else. A detector keyed on the function name
        # alone would wave it through, which is the whole reason the rule is keyed
        # on the path -- so this is a firing proof, not another exemption.
        Bites(
            "from app.transport.request_profile import _store_set_cookies\n"
            "def _finish_fetch(**kwargs):\n"
            "    return _store_set_cookies(kwargs)\n",
            "_store_set_cookies() outside _finish_fetch()",
            at=_ELSEWHERE,
        ),
    ),
    proof_path=REPO_ROOT / _COOKIE_TAIL,
)


def test_response_cookies_enter_the_jar_only_through_the_fetch_tail():
    """Cookie persistence has one door, and that door is the shared fetch tail."""
    RESPONSE_COOKIES_ENTER_VIA_THE_FETCH_TAIL.scan()


def test_cookie_tail_detector_bites():
    """Every bypass shape fires; the sanctioned two-hop chain stays clean."""
    RESPONSE_COOKIES_ENTER_VIA_THE_FETCH_TAIL.check_bites()


# --- browser instance release -------------------------------------------------
# Both the per-attempt reset and the service-shutdown close have to forget the
# same cached fields. Spelling that out twice is how one of them grows a stale
# field the other never clears -- which is exactly what happened to
# ``_browser_reused`` before _release_browser existed. Only the owner may
# clear them, so a new field cannot be added to one path and forgotten on the
# other.
_BROWSER_RELEASE_FIELDS = ("_browser_cm", "_browser", "_browser_reused")


#: The file that owns the fields and the chokepoint today, and therefore the
#: path the bite proof is planted at. Named separately from the sweep set below
#: rather than picked out of it: ``Path`` hashes through the (per-process
#: randomized) string hash, so ``next(iter(frozenset_of_paths))`` names a
#: different file on different runs and the proof would bite at random.
_BROWSER_RELEASE_OWNER = REPO_ROOT / "app" / "transport" / "browser_driver.py"


#: Both files, not one. The cached fields and the release chokepoint moved to
#: the driver when the lifecycle split, so a single-path sweep pointed at the
#: renderer reported green over a file that no longer contains any of them --
#: a gate that enforces nothing and says nothing about having stopped. Sweeping
#: the pair keeps the invariant whole across the split: a field reintroduced on
#: either side is still caught by whichever file spells the clear.
_BROWSER_RELEASE_OWNERS = frozenset({_BROWSER_RELEASE_OWNER, REPO_ROOT / "app" / "transport" / "browser.py"})


_BROWSER_RELEASE_CHOKEPOINT = "_release_browser"


#: __init__ puts the fields at their initial None; that is not a release.
_BROWSER_RELEASE_EXEMPT = frozenset({"__init__"})


def _browser_release_offenders(tree, path, *, owners, fields, chokepoint):
    # Path == Path, not path.name: comparing a str against a Path is never
    # equal, which would make the whole sweep silently yield nothing.
    if path not in owners:
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


def _browser_release_source() -> str:
    """The proof's source, with its resets generated from the swept field tuple.

    Written out twice it was the same three lines of drift-prone fixture, and the
    drift is invisible: a field added to the tuple would leave a hand-written
    proof asserting on a source that never mentions it.
    """
    reset = "\n".join(f"        self.{field} = None" for field in _BROWSER_RELEASE_FIELDS)
    return (
        "class Renderer:\n"
        f"    def __init__(self):\n{reset}\n\n"
        f"    async def {_BROWSER_RELEASE_CHOKEPOINT}(self):\n{reset}\n\n"
        "    async def close(self):\n"
        "        self._browser = None\n"
    )


BROWSER_RELEASE_ONLY_THROUGH_THE_CHOKEPOINT = Rule(
    name="browser-release-only-through-the-chokepoint",
    find=lambda tree, path: _browser_release_offenders(
        tree,
        path,
        owners=_BROWSER_RELEASE_OWNERS,
        fields=_BROWSER_RELEASE_FIELDS,
        chokepoint=_BROWSER_RELEASE_CHOKEPOINT,
    ),
    summary=(
        "_acquire_browser and close have to forget the same cached fields, and "
        "spelling that out twice is how one of them grows a stale field the other "
        "never clears; release every field through _release_browser"
    ),
    bite=lambda: Bites(
        _browser_release_source(),
        "is cleared to None in",
        at=_BROWSER_RELEASE_OWNER,
    ),
    clean=lambda: (
        # __init__ putting the fields at their initial None is not a release, and
        # neither is the chokepoint's own body.
        Silent(_browser_release_source(), at=_ELSEWHERE),
        f"class R:\n    async def {_BROWSER_RELEASE_CHOKEPOINT}(self):\n        self._browser = None\n",
    ),
)


def test_browser_instance_fields_are_cleared_only_by_the_release_chokepoint():
    BROWSER_RELEASE_ONLY_THROUGH_THE_CHOKEPOINT.scan()


def test_browser_release_detector_bites():
    BROWSER_RELEASE_ONLY_THROUGH_THE_CHOKEPOINT.check_bites()


#: The lock constructors a store must not hand itself. ``process_lock`` is the
#: only sanctioned way to serialize writes to a store, and it is keyed by the
#: database file, so every instance and every migration on that file shares one.
_PRIVATE_LOCK_NAMES = frozenset({"Lock", "RLock"})


def _opens_a_store_connection(class_node: ast.ClassDef) -> bool:
    """Whether this class reaches ``connect_store``, the one connection chokepoint.

    Scoping the rule to this call is what keeps it from being wrong rather than
    merely strict. Most of the tree holds state that no other process can reach
    -- parsed robots rules, a content cache, a cookie jar, readiness counters --
    and for all of those a private ``threading.Lock`` is the correct and only
    sensible choice. A blanket ban reports those as violations, and a rule whose
    violations include correct code is a rule people learn to ignore.

    The class reaching the chokepoint is the set the rule is actually about, and
    it is derived from the code rather than listed, so a new store is covered
    the moment it opens a connection and no exemption list can rot.
    """
    return any(
        isinstance(node, ast.Call)
        and resolved_call_name(class_node, node).rsplit(".", 1)[-1] == "connect_store"
        for node in nodes_of(class_node)
    )


def _private_store_lock_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A store must take the file's process lock, not a lock of its own.

    A per-instance ``threading.Lock`` excludes that store from nothing else. The
    migration it already ran takes ``process_lock``, and a second instance
    writing the same file brings its own private lock again -- so a write runs
    straight through a migration in flight, and the only thing left deciding
    whether that is survivable is ``busy_timeout``. That is how a shared
    database becomes ``database is locked`` raised from inside a migration step
    on one run in twelve, in some other worker's test, with no way to reproduce
    it on demand.

    It is invisible by construction: a private lock is locally correct, passes
    every single-store test, and only fails once a second writer exists. So the
    rule has to be a gate rather than a review convention.
    """
    offenders: list[str] = []
    for class_node in class_defs(tree):
        if not _opens_a_store_connection(class_node):
            continue
        for node in writes(class_node):
            value = node.value
            if not (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)):
                continue
            if value.func.attr not in _PRIVATE_LOCK_NAMES:
                continue
            if not (isinstance(value.func.value, ast.Name) and value.func.value.id == "threading"):
                continue
            targets = assignment_targets(node)
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "_lock":
                    offenders.append(
                        offender_at(
                            path,
                            node,
                            f"{class_node.name} serializes on a private lock, not process_lock",
                        )
                    )
    return offenders


STORES_SERIALIZE_ON_THE_FILES_PROCESS_LOCK = Rule(
    name="stores-serialize-on-the-files-process-lock",
    find=_private_store_lock_offenders,
    summary=(
        "A per-instance threading.Lock excludes that store from no other process, "
        "so a write can run straight through a migration in flight; serialize on "
        "process_lock(store_lock_key(db_path))"
    ),
    proof_path=REPO_ROOT / "app" / "crawl" / "store.py",
    bite=lambda: (
        Bites(
            "class Store:\n"
            "    def __init__(self, db_path):\n"
            "        self._conn = connect_store(db_path, schema=schema)\n"
            "        self._lock = threading.Lock()\n",
            "private lock, not process_lock",
        ),
        Bites(
            "class Store:\n"
            "    def __init__(self, db_path):\n"
            "        self._conn = connect_store(db_path, schema=schema)\n"
            "        self._lock = threading.RLock()\n",
            "private lock, not process_lock",
        ),
    ),
    clean=lambda: (
        "class Store:\n"
        "    def __init__(self, db_path):\n"
        "        self._lock = process_lock(store_lock_key(db_path))\n"
        "        self._conn = connect_store(db_path, schema=schema)\n",
        # A module-level guard is not a store's write lock, and banning it would
        # be the rule being wrong rather than the code.
        "_PROCESS_LOCKS_GUARD = threading.Lock()\n",
        # State no other process can reach. A private lock is the right answer
        # here, which is the whole reason the rule is scoped to the connection
        # chokepoint rather than banning the constructor.
        "class RobotsCache:\n    def __init__(self):\n        self._lock = threading.Lock()\n",
    ),
)


def test_stores_serialize_on_the_files_process_lock():
    STORES_SERIALIZE_ON_THE_FILES_PROCESS_LOCK.scan()


def test_private_store_lock_detector_bites():
    STORES_SERIALIZE_ON_THE_FILES_PROCESS_LOCK.check_bites()
