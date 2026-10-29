"""State that is acquired is released by the thing that owns it.

Locks and caches: no await under a lock, no twin without the helper."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, call_nodes, nodes_of, offender_at, subscript_reads, with_blocks
from tests.invariants._shared import function_defs
from tests.rules import Bites, Registry, Rule


def _lock_bodies_with_awaits(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``with <lock>:`` bodies containing an await.

    A threading lock held across an await blocks the loop; only sync ``with``
    is checked (``async with`` may await). Locks are matched by name or binding.
    """
    offenders: list[str] = []
    lock_names = _threading_lock_bindings(tree)
    for node in with_blocks(tree):
        if isinstance(node, ast.AsyncWith):
            continue  # asyncio.Lock bodies may await by design
        for item in node.items:
            # `with self._lock:` / `with _CLOSE_LOCK:` / `with guard:` -- a sync lock context. Any await
            # inside is a loop-blocker.
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


_OWNER_PATH = REPO_ROOT / "crawlutron" / "transport" / "browser.py"


_OWNER_MODULE = "crawlutron/transport/browser.py"


LOCK_HELD_ACROSS_AN_AWAIT = Rule(
    name="lock-held-across-an-await",
    find=_lock_bodies_with_awaits,
    summary=(
        "A threading lock held across an await blocks the event loop; route the "
        "asynchronous wait outside the `with` and take only the lock inside it"
    ),
    bite=lambda: (
        # A name stem, and a name bound to a threading lock-family constructor: the second is the one a rename
        # tries to escape, since self._mutex is not a lock by name at all.
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
        # asyncio.Lock bodies may await -- that IS the sanctioned pattern, and the only reason the sweep reads
        # `with` and skips `async with`.
        "import asyncio\n\nclass C:\n    def __init__(self):\n        self._lock = asyncio.Lock()\n"
        "    async def go(self):\n        async with self._lock:\n            await x()\n",
        # A context manager that is not a lock is not this rule's business.
        "def go():\n    with open('f'):\n        pass\n",
    ),
)


# --------------------------------------------------------------------------- ContentCache mirror twins
# ---------------------------------------------------------------------------

_CACHE_TWIN_METHODS = {"get", "aget", "set", "aset"}


def _memory_cache_twin_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag public cache twin methods touching ``_memory_cache`` directly.

    The RAM mirror logic must live in the ``_*_locked`` helpers so the sync and
    async paths cannot drift (aget once lost the LRU cap -- an invisible twin).
    """
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


# --------------------------------------------------------------------------- Lock ownership: a *_locked
# helper presumes the caller holds the lock


def _is_locked_helper_name(name: str | None) -> bool:
    # ``name is not None`` rather than ``bool(name)``: both reject the empty string, but only the first states
    # the narrowing the two ``str`` methods below then rely on.
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

    The helpers assume the caller holds ``self._lock``; ``get()`` once called
    one off-lock while ``aget()`` held it -- drift the twin detector can't see.
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


RULES = Registry(
    (
        LOCK_HELD_ACROSS_AN_AWAIT,
        MEMORY_CACHE_TWINS_MUST_GO_THROUGH_THE_LOCKED_HELPERS,
        LOCKED_HELPERS_ARE_CALLED_UNDER_THE_LOCK,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()
