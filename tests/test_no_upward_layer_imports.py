"""Capability layers never import up into orchestration.

Orchestration decides *what to do*; extraction and admission are the layers it
calls. A lower layer importing orchestration inverts the dependency (the engine
imports extraction and admission; they must not import the engine's home
package). Ownership moves fix violations; this gate refuses new ones. The gate
widens per ticket: #163 (extraction) here now, #162 (admission) next; the
transport band (browser/browser_pool/browser_result/fetch_policy/http) is the
same shape tracked separately.
"""

from __future__ import annotations

from scripts.module_graph import build_graph

_LOWERS = ("app.extraction",)


def _orchestration_imports() -> list[str]:
    """Every module-level import edge from a lower layer into app.orchestration."""
    modules, _importers = build_graph()
    return sorted(
        f"{src} -> {dst}"
        for src, deps in modules.items()
        if src.startswith(_LOWERS)
        for dst in deps
        if dst.startswith("app.orchestration")
    )


def test_lower_layers_import_nothing_from_orchestration() -> None:
    offenders = _orchestration_imports()
    assert not offenders, "\n".join(
        [
            "lower layers import app.orchestration (ownership move required, "
            "never a lazy import or a facade):",
            *offenders,
        ]
    )


def test_gate_bites_on_a_planted_upward_import() -> None:
    planted = {
        "app.extraction.planted_up": {"app.orchestration.engine"},
        "app.orchestration.engine": set(),
    }
    found = sorted(
        f"{src} -> {dst}"
        for src, deps in planted.items()
        if src.startswith(_LOWERS)
        for dst in deps
        if dst.startswith("app.orchestration")
    )
    assert found == ["app.extraction.planted_up -> app.orchestration.engine"]
