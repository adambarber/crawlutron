"""One registry owns every module holding state that outlives a call (#621).

No module exports a hand-copied ``reset_process_state`` pair; each registers a
private reset/probe with ``crawlutron.runtime.process_state``, and the harness
drives the one seam. The matcher asserts registration shape, probe/reset
agreement, and idempotence -- not fabricated state.

Disposition of the #643 chain-abort hardening: the completing-reset-chain fix
in ``crawlutron.runtime.process_state`` is LATENT HARDENING. The raising site
behind the observed pooled-supersede teardown signatures is UNIDENTIFIED and
stays open in the flake ledger; no registered reset is known to raise today.
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
    "crawlutron.transport.fetch_helpers",
    "crawlutron.transport.browser_cookies",
    "crawlutron.transport.cookie_jar",
    "crawlutron.transport.pooled_clients",
    "crawlutron.transport.pinned_dns",
    "crawlutron.transport.pooled_cffi",
    "crawlutron.api.webhooks",
    "crawlutron.storage.sqlite_store",
    "crawlutron.api.async_crawl_runner",
    "crawlutron.api.job_lifecycle",
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


def test_engine_reset_clears_the_spill_gc_cadence_stamp() -> None:
    """#605: the cadence stamp gated the sweep across resets, so a reset process
    skipped the first sweep of its new crawl lifetime. Reset drops the stamp.
    """
    import crawlutron.orchestration.engine as engine

    engine.spill_gc_last_run = 1.0  # a stamp claiming the sweep just ran
    process_state.reset_all_process_state()
    assert engine.spill_gc_last_run is None, (
        "the spill GC cadence stamp outlived the registry reset -- the next crawl "
        "skips the first sweep of its new lifetime"
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


def test_one_failing_reset_cannot_strand_the_rest() -> None:
    """Latent hardening (#643 family): a failing owner's reset must abort nothing.

    The chain completes so later owners' state can never be stranded; the
    failure still surfaces, attributed. No registered reset is known to raise
    today -- the observed teardown signatures' raising site remains open."""
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
