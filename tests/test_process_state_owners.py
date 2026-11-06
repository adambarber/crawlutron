"""Every module holding state that outlives a call satisfies one protocol (six
owners reachable through it, not sixteen private conftest names); the matcher is
parameterized over the table and asserts shape, agreement, and idempotence -- not fabricated state.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from crawlutron.runtime.process_state import ProcessStateOwner
from tests.chokepoint_walks import REPO_ROOT

OWNERS: tuple[str, ...] = (
    "crawlutron.runtime.cgroup_sense",
    "crawlutron.orchestration.engine",
    "crawlutron.transport.fetch_helpers",
    "crawlutron.transport.browser_cookies",
    "crawlutron.transport.cookie_jar",
    "crawlutron.transport.pooled_clients",
    "crawlutron.transport.pinned_dns",
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
    """The probe reads what the reset empties: a live check needs real module state,
    which means reaching into the private store, so the pair is checked structurally
    instead -- disjoint names would disagree.
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


def test_engine_reset_empties_the_shared_content_cache() -> None:
    """The fourth process-global sits inside the reset/probe pair, not beside it.

    #147: the shared content cache was emptied by nothing and seen by nothing.
    A seeded entry survived both halves.
    """
    import crawlutron.orchestration.engine as engine

    cache = engine.get_shared_content_cache()
    cache.set("engine-reset-probe", {"data": "value"})
    assert engine.has_process_state(), "a seeded content cache is live process state"
    engine.reset_process_state()
    assert not engine.has_process_state(), (
        "the shared content cache outlived the reset that empties every shared cache"
    )


def test_every_owner_is_listed() -> None:
    """The table is the declaration; a new module must be added to it, asserted against
    the tree rather than trusted -- an unnamed state holder conforms to nothing and is
    checked by nothing, which is how the six undocumented reaches survived.
    """
    listed = {name.rpartition(".")[2] for name in OWNERS}
    found: set[str] = set()
    for path in sorted((REPO_ROOT / "crawlutron").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if {"reset_process_state", "has_process_state"} <= names:
            found.add(pathlib.Path(*path.relative_to(REPO_ROOT).parts).stem)
    assert found <= listed, f"modules conforming but not listed in OWNERS: {sorted(found - listed)}"


def test_engine_reset_clears_the_spill_gc_cadence_stamp() -> None:
    """#605: the cadence stamp gated the sweep across resets, so a reset process
    skipped the first sweep of its new crawl lifetime. Reset drops the stamp.
    """
    import crawlutron.orchestration.engine as engine

    engine.spill_gc_last_run = 1.0  # a stamp claiming the sweep just ran
    engine.reset_process_state()
    assert engine.spill_gc_last_run is None, (
        "the spill GC cadence stamp outlived the reset that empties every shared "
        "cache -- the next crawl skips the first sweep of its new lifetime"
    )
