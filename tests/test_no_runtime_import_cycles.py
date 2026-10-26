"""#542: zero module-level import cycles, gated here (see scripts/module_graph)."""

from __future__ import annotations

from scripts.module_graph import build_graph, cycles


def test_no_runtime_import_cycles() -> None:
    """Zero module-level import cycles; the message names every offender chain.

    Red-first proof: at pre-#542 HEAD this failed with the 9 existing cycles,
    each resolved by an ownership move (see issue #542 body).
    """
    modules, _importers = build_graph()
    found = cycles(modules)
    offenders = "\n".join(" -> ".join(cycle) for cycle in found)
    assert not found, f"module-level import cycles detected ({len(found)}):\n{offenders}"


def test_gate_bites_on_a_planted_cycle() -> None:
    """The gate is not vacuous: a planted two-module runtime cycle goes red."""
    planted = {
        "app.api.planted_a": {"app.api.planted_b", "app.vocabulary.taxonomy"},
        "app.api.planted_b": {"app.api.planted_a"},
        "app.vocabulary.taxonomy": set(),
    }
    found = cycles(planted)
    offenders = "\n".join(" -> ".join(cycle) for cycle in found)
    assert found, "expected the planted cycle to be detected"
    assert any("app.api.planted_a" in cycle and "app.api.planted_b" in cycle for cycle in found), offenders
