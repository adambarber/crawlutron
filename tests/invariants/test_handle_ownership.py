"""Handles, floors, sweeps and semaphores have one owner."""

import ast
import pathlib

from tests.chokepoint_walks import (
    assignment_targets,
    calls_named_leaf,
    functions,
    name_reads,
    nodes_of,
    offender_at,
    subscript_reads,
    unannotated_writes,
)
from tests.invariants._shared import _enclosing_defs
from tests.rules import Bites, Rule, Trips

# --------------------------------------------------------------------------- Handle minting is single-sourced
# (entropy policy)


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


# --------------------------------------------------------------------------- Robots politeness floor is
# committed only through the bounded setter


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
        # The setter's clamped write and a read are both fine: the rule is about who may WRITE, and an
        # augmented assignment is still a write.
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


# --------------------------------------------------------------------------- Age-based deletion must consult
# ownership, not just age
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
        # A method named for cleanup still has to ask; the name is a trigger, not a grant.
        Bites(
            "class C:\n    def cleanup(self):\n        os.remove(self.path)\n",
            "unlinks without an ownership check",
        ),
    ),
    clean=lambda: (
        # The guard has to be something the KERNEL can answer -- ownership another process can also see -- not
        # a set this process happens to be holding.
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
