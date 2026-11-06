"""One registry owns every module holding state that outlives a call (#621).

No module exports a hand-copied ``reset_process_state`` pair; each registers a
private reset/probe with ``crawlutron.runtime.process_state``, and the harness
drives the one seam. The matcher asserts registration shape, probe/reset
agreement, and idempotence -- not fabricated state.
"""

from __future__ import annotations

import ast
import importlib

import pytest

from crawlutron.runtime import process_state

# Registered owners the registry tests reach into directly.
KNOWN = (
    "crawlutron.runtime.cgroup_sense",
    "crawlutron.orchestration.engine",
    "crawlutron.transport.fetch_helpers",
    "crawlutron.transport.browser_cookies",
    "crawlutron.transport.cookie_jar",
    "crawlutron.transport.pooled_clients",
    "crawlutron.transport.pinned_dns",
    "crawlutron.transport.pooled_cffi",
    "crawlutron.api.webhooks",
    "crawlutron.storage.sqlite_store",
    "crawlutron.api.async_crawl_runner",
)


def _module_level_state(module: ast.Module) -> set[str]:
    """Names bound once at module level and therefore shared by every caller."""
    bound: set[str] = set()
    for node in module.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound.update(t.id for t in targets if isinstance(t, ast.Name))
    return bound


@pytest.mark.parametrize("module_path", KNOWN)
def test_owner_registered_its_pair(module_path: str) -> None:
    importlib.import_module(module_path)
    process_state.owner_has_state(module_path)  # raises KeyError when unregistered


@pytest.mark.parametrize("module_path", KNOWN)
def test_reset_is_idempotent(module_path: str) -> None:
    importlib.import_module(module_path)
    process_state.reset_all_process_state()
    process_state.reset_all_process_state()
    assert not process_state.owner_has_state(module_path), (
        f"{module_path} still holds process state right after the registry reset -- "
        f"a probe that cannot say False certifies nothing"
    )


def test_every_owner_registers_and_none_exports_the_pair() -> None:
    """The registry is the declaration: a module with the reset shape must register,
    and no app module may export the hand-copied pair -- the seam is the only door.
    """
    from tests.invariants.test_reset_family import _hand_copied_pair_offenders

    offenders = _hand_copied_pair_offenders()
    assert not offenders, (
        f"modules hand-copying reset_process_state/has_process_state: {offenders} -- "
        f"register with crawlutron.runtime.process_state instead"
    )


def test_conformers_register() -> None:
    """Modules holding reset-shaped state are all in the registry (bite of the inverse)."""
    for module_path in KNOWN:
        importlib.import_module(module_path)
    assert process_state.registered_owners(), "the registry is empty: every owner went unregistered"


def test_registry_reset_actually_runs_every_registration() -> None:
    """A registration the sweep skips is a reset that silently never happened."""
    from crawlutron.runtime import process_state

    calls: list[str] = []
    process_state.register("test-fake-a", lambda: calls.append("a"), lambda: bool(calls))
    process_state.register("test-fake-b", lambda: calls.append("b"), lambda: bool(calls))
    process_state.reset_all_process_state()
    assert calls == ["a", "b"], f"the registry reset ran {calls}, not every registration"


def test_probe_agrees_with_reset() -> None:
    """A probe that says False over populated state certifies a failed reset."""
    from crawlutron.runtime import process_state

    holder = {"k": 1}
    process_state.register("test-fake-probe", lambda: holder.clear(), lambda: bool(holder))
    assert process_state.owner_has_state("test-fake-probe"), "the probe lied about populated state"
    process_state.reset_all_process_state()
    assert not process_state.owner_has_state("test-fake-probe"), "the reset emptied nothing"


def test_no_straggler_defines_the_pair() -> None:
    """No app module may define reset_process_state/has_process_state at module level:
    the pair lives once, in the registry module."""
    from tests.invariants.test_reset_family import _hand_copied_pair_offenders

    offenders = _hand_copied_pair_offenders()
    assert not offenders, f"modules still defining the hand-copied pair: {offenders}"
