"""Every module holding state that outlives a call satisfies one protocol.

Six modules do: ``pinned_transport`` (DNS answers), ``cookie_jar`` (the process jar
and the request namespace), ``fetch_helpers`` (sitemap documents), ``engine`` (the
robots and shared HTTP caches), ``http_client_pool`` (the two lazily-created
clients and the pending-close queue), and ``cgroup_sense`` (memoized cgroup reads).
Before this table existed, the harness reached into all six -- sixteen private names
from ``tests/conftest.py`` and seven hand-rolled ``.clear()`` calls across four test
files -- because a caller obliged to publish a reset had no door to publish it
through.

The matcher is parameterized over the table rather than written out per module,
because the claim is a property of the *protocol*: every owner answers the same
question the same way. A test per module would pass identically while the protocol
drifted, which is the failure this file exists to prevent.

What is asserted, and what is deliberately not
----------------------------------------------

Three things, all checkable without a network:

1. **Shape.** Each owner satisfies :class:`~app.process_state.ProcessStateOwner`.
2. **Agreement.** The probe and the reset name the same module-level state. A probe
   reading a different object than the reset empties would certify a reset that
   never emptied anything, and no live assertion catches that -- see below.
3. **Idempotence.** Resetting twice is the same as resetting once, because the
   harness calls it before every test and a caller that has already reset must not
   be punished for it.

What is *not* asserted is that the probe returns ``True`` when the module holds
something. Arranging real state means a DNS resolution, a fetch, or a client build
-- and the only cheap way to do it is to write into the module's private store,
which is the exact reach-in this program exists to remove. A test that fabricates
the state it then verifies has proved that a dict is clearable. So the live half of
the contract is stated here and checked by the owner instead, which is why each
``has_process_state`` is written against the same object its ``reset_process_state``
empties and the gate above refuses a pair that disagrees.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from app.process_state import ProcessStateOwner
from tests.chokepoint_walks import REPO_ROOT

OWNERS: tuple[str, ...] = (
    "app.cgroup_sense",
    "app.crawl.engine",
    "app.crawl.fetch_helpers",
    "app.crawl.strategies.browser",
    "app.crawl.strategies.cookie_jar",
    "app.crawl.strategies.http_client_pool",
    "app.crawl.strategies.pinned_transport",
)


def _module_level_state(module: ast.Module) -> set[str]:
    """Names bound once at module level and therefore shared by every caller."""
    bound: set[str] = set()
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.ClassDef):
            bound.add(node.name)
    return bound


def _names_read(fn: ast.FunctionDef) -> set[str]:
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


@pytest.mark.parametrize("module_path", OWNERS)
def test_owner_satisfies_the_protocol(module_path: str) -> None:
    assert isinstance(importlib.import_module(module_path), ProcessStateOwner)


@pytest.mark.parametrize("module_path", OWNERS)
def test_probe_and_reset_agree_on_the_state(module_path: str) -> None:
    """The probe reads what the reset empties.

    The one part of the contract a live run cannot check cheaply. Asserting it
    needs real state in the module, and manufacturing that means writing to the
    private store -- so the pair is checked structurally instead: two functions
    naming disjoint state are two functions answering about different objects, and
    the one that reports emptiness would be reporting on something else.
    """
    module = ast.parse((REPO_ROOT / f"{module_path.replace('.', '/')}.py").read_text(encoding="utf-8"))
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    state = _module_level_state(module)
    reset = _names_read(functions["reset_process_state"]) & state
    probe = _names_read(functions["has_process_state"]) & state

    assert reset, f"{module_path}.reset_process_state names no module-level state to empty"
    assert reset & probe, (
        f"{module_path}: reset empties {sorted(reset)} but has_process_state reads {sorted(probe)} "
        f"-- disjoint, so the probe certifies a reset that emptied nothing"
    )


@pytest.mark.parametrize("module_path", OWNERS)
def test_reset_is_idempotent(module_path: str) -> None:
    module = importlib.import_module(module_path)
    module.reset_process_state()
    module.reset_process_state()
    assert not module.has_process_state(), (
        f"{module_path}.has_process_state() is True on a module that was just reset -- "
        f"a probe that can only say False satisfies every assertion in this file"
    )


def test_every_owner_is_listed() -> None:
    """The table is the declaration; a new module has to be added to it.

    Asserted against the tree rather than trusted: a module that gains
    process-wide state and is not named here conforms to nothing and is checked
    by nothing, which is how the six undocumented reaches in ``conftest`` survived
    a gate that was already looking for them.
    """
    listed = {name.rpartition(".")[2] for name in OWNERS}
    found: set[str] = set()
    for path in sorted((REPO_ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if {"reset_process_state", "has_process_state"} <= names:
            found.add(pathlib.Path(*path.relative_to(REPO_ROOT).parts).stem)
    assert found <= listed, f"modules conforming but not listed in OWNERS: {sorted(found - listed)}"
