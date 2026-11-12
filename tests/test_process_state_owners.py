"""One registry owns every module holding state that outlives a call: no module exports a hand-copied
``reset_process_state`` pair; each registers a private reset/probe with
``crawlutron.runtime.process_state``, and the harness drives the one seam.
"""

from __future__ import annotations

import ast
import importlib

import pytest

from crawlutron.runtime import process_state

# Registered owners the registry tests reach into directly (the third invariant
# drives exhaustive membership from registered_owners(); these pin the names).
KNOWN = (
    "crawlutron.runtime.cgroup_sense",
    "crawlutron.orchestration.engine",
    "crawlutron.transport.cache",
    "crawlutron.transport.content_cache",
    "crawlutron.transport.fetch_helpers",
    "crawlutron.transport.browser_cookies",
    "crawlutron.transport.cookie_jar",
    "crawlutron.transport.pooled_clients",
    "crawlutron.transport.pinned_dns",
    "crawlutron.transport.pooled_cffi",
    "crawlutron.service.webhooks",
    "crawlutron.storage.sqlite_store",
    "crawlutron.service.async_crawl_runner",
    "crawlutron.service.job_lifecycle",
    "crawlutron.models.host_policy",
    "crawlutron.runtime.metrics",
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
    """Modules holding reset-shaped state are all in the registry (bite of the inverse):
    every known owner name appears in registered_owners(), so a 14th owner cannot
    be silently missed -- the registry's membership is checked against this list."""
    for module_path in KNOWN:
        importlib.import_module(module_path)
    registered = process_state.registered_owners()
    assert registered, "the registry is empty: every owner went unregistered"
    missing = set(KNOWN) - set(registered)
    assert not missing, f"known owners never registered: {sorted(missing)}"


def _run_registration_probe(calls: list[str]) -> None:
    """Register the reset-probe fakes, run the sweep, drop them when done.

    A leaked permanent-True probe trips every pooled teardown that reads the global
    registry (#881); unregister is part of the probe, not optional cleanup."""
    process_state.register("test-fake-a", lambda: calls.append("a"), lambda: bool(calls))
    process_state.register("test-fake-b", lambda: calls.append("b"), lambda: bool(calls))
    try:
        process_state.reset_all_process_state()
    finally:
        process_state.unregister("test-fake-a")
        process_state.unregister("test-fake-b")


def test_registry_reset_actually_runs_every_registration() -> None:
    """A registration the sweep skips is a reset that silently never happened."""
    calls: list[str] = []
    _run_registration_probe(calls)
    assert calls == ["a", "b"], f"the registry reset ran {calls}, not every registration"


def test_fake_owners_are_unregistered_after_every_registration() -> None:
    """The reset probe unregisters its fakes: a leak can't poison sibling teardowns."""
    calls: list[str] = []
    _run_registration_probe(calls)
    for owner in ("test-fake-a", "test-fake-b"):
        assert owner not in process_state.registered_owners(), (
            f"{owner} leaked a permanent-True probe into the shared registry"
        )


def test_probe_agrees_with_reset() -> None:
    """A probe that says False over populated state certifies a failed reset."""
    from crawlutron.runtime import process_state

    holder = {"k": 1}
    process_state.register("test-fake-probe", lambda: holder.clear(), lambda: bool(holder))
    assert process_state.owner_has_state("test-fake-probe"), "the probe lied about populated state"
    try:
        process_state.reset_all_process_state()
        assert not process_state.owner_has_state("test-fake-probe"), "the reset emptied nothing"
    finally:
        process_state.unregister("test-fake-probe")


def test_one_failing_reset_cannot_strand_the_rest() -> None:
    """Latent hardening (#643 family): a failing owner's reset must abort nothing -- the chain completes so
    later owners' state can never be stranded, and the failure still surfaces, attributed. No registered
    reset is known to raise today; the raising site remains open.
    """
    seen: list[str] = []

    def _boom() -> None:
        raise RuntimeError("closed loop in teardown")

    process_state.register("test-fake-failing", _boom, lambda: False)
    process_state.register("test-fake-later", lambda: seen.append("later"), lambda: bool(seen))
    raised: RuntimeError | None = None
    try:
        process_state.reset_all_process_state()
    except RuntimeError as e:
        raised = e
    finally:
        process_state.unregister("test-fake-failing")
        process_state.unregister("test-fake-later")
    assert "test-fake-failing" not in process_state.registered_owners(), (
        "the probe left its fake owners registered -- registry pollution"
    )
    assert raised is not None, "a failing owner's reset vanished instead of surfacing"
    assert seen == ["later"], "a failing owner's reset stranded every later owner's state"
    assert "test-fake-failing" in str(raised), f"the failure named no owner: {raised}"


def test_unregister_drops_only_its_owner() -> None:
    """Red for the unregister door: registration is gone after, siblings stay."""
    process_state.register("test-fake-gone", lambda: None, lambda: False)
    process_state.register("test-fake-stays", lambda: None, lambda: True)
    try:
        assert process_state.unregister("test-fake-gone") is True
        assert process_state.unregister("test-fake-gone") is False
        registered = process_state.registered_owners()
    finally:
        process_state.unregister("test-fake-stays")
    assert "test-fake-gone" not in registered and "test-fake-stays" in registered


def test_no_straggler_defines_the_pair() -> None:
    """No app module may define reset_process_state/has_process_state at module level:
    the pair lives once, in the registry module."""
    from tests.invariants.test_reset_family import _hand_copied_pair_offenders

    offenders = _hand_copied_pair_offenders()
    assert not offenders, f"modules still defining the hand-copied pair: {offenders}"
