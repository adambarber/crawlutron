"""Handles, floors, sweeps and semaphores have one owner."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
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
from tests.rules import Bites, Registry, Rule, Trips

# --------------------------------------------------------------------------- Handle minting is single-sourced
# (entropy policy)


def _id_mint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct ``uuid4()`` calls or ``.hex`` slicing outside crawlutron/storage/ids.

    A re-inlined mint is where a shorter slice regresses to 48 bits; both
    shapes are separate walks because a call and a subscript are different nodes.
    """
    return [
        offender_at(path, call, "uuid4() minted outside crawlutron/storage/ids.py")
        for call in calls_named_leaf(tree, {"uuid4"})
    ] + [
        offender_at(path, node, "uuid hex sliced outside crawlutron/storage/ids.py")
        for node in subscript_reads(tree)
        if getattr(node.value, "attr", None) == "hex"
    ]


HANDLE_MINTING_IS_SINGLE_SOURCED = Rule(
    name="handle-minting-is-single-sourced",
    find=_id_mint_offenders,
    summary=(
        "Handles double as the lookup credential for the status/result routes, "
        "so a re-inlined mint is where a shorter slice silently regresses to 48 "
        "bits; mint through crawlutron/storage/ids.py"
    ),
    skip=frozenset({"crawlutron/storage/ids.py"}),  # owner: the single mint (entropy policy, 48-bit floor)
    bite=lambda: Trips(
        "import uuid\n\ndef f():\n    return uuid.uuid4().hex[:12]\n",
        count=2,
    ),
    clean=lambda: (
        "from crawlutron.storage.ids import new_request_id\n\ndef f():\n    return new_request_id()\n",
    ),
)


# --------------------------------------------------------------------------- Robots politeness floor is
# committed only through the bounded setter


def _unbounded_domain_delay_writes(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag direct writes to the domain's ``min_delay`` outside ``set_domain_min_delay``.

    Only that setter clamps the robots Crawl-delay floor; a bare write
    elsewhere drops an ``inf`` straight into ``asyncio.sleep``. #577 moved the
    state into ``DomainState.min_delay``; the detector follows the field, not
    the old map name.
    """
    offenders: list[str] = []
    # __init__ is the field's zero default, not a floor installation; only the clamped setter may raise it.
    # observation_copy restates the source's OWN already-clamped value -- it installs no new floor.
    exempt = {"set_domain_min_delay", "__init__", "observation_copy"}
    for func in (
        node for node in nodes_of(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        if func.name in exempt:
            continue
        for node in unannotated_writes(func):
            targets = assignment_targets(node)
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr == "min_delay":
                    offenders.append(
                        offender_at(
                            path,
                            node,
                            f"unbounded write to the politeness floor in {func.name}()",
                        )
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
        "class S:\n    def other(self):\n        self._state('x').min_delay = 1e9\n"
        "    def bump(self):\n        self._state('y').min_delay += 1\n",
        count=2,
    ),
    clean=lambda: (
        # The setter's clamped write and a read are both fine: the rule is about who may WRITE, and an
        # augmented assignment is still a write.
        "class S:\n    def set_domain_min_delay(self, d, s):\n"
        "        self._state(d).min_delay = min(s, 300.0)\n"
        "    def read(self, d):\n        return self._state(d).min_delay\n",
    ),
)


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


# --- 4c: the webhook semaphore lives in one loop-bound holder ---------------

#: The one holder name: it pairs the semaphore with the loop that built it.
_SEMAPHORE_HOLDER_GLOBAL = "_SEMAPHORE_HOLDER"

# The accessor rebinds the holder to the running loop; the registered reset empties
# it so a test swapping the builder observes its patch immediately.
_SEMAPHORE_WRITERS = frozenset({"async_crawl_semaphore", "_reset", "_has"})


def _semaphore_holder_touch_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """The holder's name is touched only by its two writers.

    The holder pairs the semaphore with the loop that built it; reaching around
    the accessor is how a Semaphore gets awaited off its own loop.
    """
    return [
        offender_at(
            path,
            node,
            f"{node.id} touched outside {'/'.join(sorted(_SEMAPHORE_WRITERS))}; "
            f"use the accessor so the semaphore stays bound to its loop",
        )
        for node in name_reads(tree)
        if node.id == _SEMAPHORE_HOLDER_GLOBAL
        and (scopes := _enclosing_defs(node, tree))
        and not set(scopes).intersection(_SEMAPHORE_WRITERS)
    ]


SEMAPHORE_HOLDER_IS_SINGLE_AND_LOOP_BOUND = Rule(
    name="semaphore-holder-is-single-and-loop-bound",
    find=_semaphore_holder_touch_offenders,
    summary=(
        "The webhook semaphore and its loop live in one holder, and only the "
        "accessor and the reset may touch it; reaching around the accessor is "
        "how a Semaphore gets awaited off its own loop"
    ),
    bite=lambda: (
        Bites(
            "async def f():\n    return _SEMAPHORE_HOLDER.semaphore.acquire()\n",
            "use the accessor so the semaphore stays bound to its loop",
        ),
        # A direct rebind is the same bypass.
        Bites(
            "def f():\n    global _SEMAPHORE_HOLDER\n    _SEMAPHORE_HOLDER = None\n",
            "use the accessor so the semaphore stays bound to its loop",
        ),
    ),
    clean=lambda: (
        # The module-level construction is the definition, not a touch.
        "_SEMAPHORE_HOLDER = _SemaphoreHolder()\n",
        # The accessor itself is the sanctioned rebinding writer.
        "def async_crawl_semaphore():\n"
        "    global _SEMAPHORE_HOLDER\n"
        "    _SEMAPHORE_HOLDER = _SemaphoreHolder(_build(), None)\n"
        "    return _SEMAPHORE_HOLDER.semaphore\n",
        # The private registration reset is the other sanctioned writer: it empties the holder.
        "def _reset():\n    global _SEMAPHORE_HOLDER\n    _SEMAPHORE_HOLDER = _SemaphoreHolder()\n",
    ),
)


RULES = Registry(
    (
        HANDLE_MINTING_IS_SINGLE_SOURCED,
        DOMAIN_POLITENESS_FLOOR_ONLY_VIA_THE_SETTER,
        AGE_BASED_DELETION_CONSULTS_OWNERSHIP,
        SEMAPHORE_HOLDER_IS_SINGLE_AND_LOOP_BOUND,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()


# The holder's runtime semantics, locked where the AST rule locks its shape.


def test_the_holder_is_one_value_with_exactly_the_two_fields():
    """Single holder: the semaphore and its loop are one dataclass value, not two globals."""
    source = (REPO_ROOT / "crawlutron/api/async_crawl_runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    holders = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "_SemaphoreHolder"
    ]
    assert len(holders) == 1, "the holder type must be defined exactly once"
    fields = [
        stmt.target.id
        for stmt in holders[0].body
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
    ]
    assert fields == ["semaphore", "loop"], "the holder carries exactly the semaphore and its loop"


def test_the_holder_is_loop_bound_and_reset_clears_it():
    """A foreign running loop forces a rebuild through the public accessor, and
    the public reset empties the holder so the next accessor rebuilds fresh."""
    import asyncio

    import crawlutron.api.async_crawl_runner as runner
    from crawlutron.runtime import process_state

    async def _accessor_on_this_loop():
        return runner.async_crawl_semaphore()

    process_state.reset_all_process_state()
    first = runner.async_crawl_semaphore()
    foreign = asyncio.new_event_loop()
    try:
        rebuilt = foreign.run_until_complete(_accessor_on_this_loop())
        assert rebuilt is not first, "a loop change must rebuild, not reuse"
    finally:
        foreign.close()
    process_state.reset_all_process_state()
    assert runner.async_crawl_semaphore() is not first, "post-reset access must rebuild"
