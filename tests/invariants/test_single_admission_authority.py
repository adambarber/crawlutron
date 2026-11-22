"""One admission decision: the kernel owns ramp, floor, scale and liveness."""

from __future__ import annotations

import ast
import importlib.util
from collections.abc import Iterable

from tests.chokepoint_paths import REPO_ROOT
from tests.chokepoint_walks import nodes_of, offender_at, trees_under
from tests.rules import Bites, Rule, Silent

_ORCHESTRATION = REPO_ROOT / "packages/core/src/crawlutron/orchestration"
_SYNTHETIC = _ORCHESTRATION / "_synthetic.py"
_ELSEWHERE = REPO_ROOT / "packages/crawlutron-admit/src/crawlutron_admit/gate.py"


def _caller_side_authority_offenders(tree: ast.Module, path) -> Iterable[str]:
    """Every caller-side scale/ramp reach in an orchestration module."""
    offenders: list[str] = []
    seen: set[tuple[int, str]] = set()
    for node in nodes_of(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"batch_scale_factor", "http_scale_factor"}:
            key = (node.lineno, "caller-side scale belongs to the kernel gate")
            if key not in seen:
                seen.add(key)
                offenders.append(offender_at(path, node, key[1]))
        if isinstance(node, ast.Attribute) and node.attr == "ramp" and isinstance(node.value, ast.Name):
            if node.value.id == "state":
                key = (node.lineno, "caller-owned ramp belongs to the kernel gate")
                if key not in seen:
                    seen.add(key)
                    offenders.append(offender_at(path, node, key[1]))
    return offenders


CALLER_SIDE_AUTHORITY = Rule(
    name="orchestration-leaves-admission-decisions-to-the-kernel",
    find=_caller_side_authority_offenders,
    summary="caller-side scale or ramp: the kernel gate is the one decision authority",
    scope=lambda *, exclude: trees_under(_ORCHESTRATION),
    proof_path=_SYNTHETIC,
    bite=lambda: (
        Bites("scale = gate.batch_scale_factor()\n", "caller-side scale"),
        Bites("state.ramp = state.ramp * 1.10\n", "caller-owned ramp"),
        Bites("scale = gate.http_scale_factor()\n", "caller-side scale"),
    ),
    clean=lambda: (
        Silent("scale = granted.scale.value\n", at=_SYNTHETIC),
        Silent("gate._ramp = 1\n", at=_ELSEWHERE),
    ),
)

RULES = (CALLER_SIDE_AUTHORITY,)


def test_orchestration_leaves_admission_decisions_to_the_kernel() -> None:
    """No caller-side scale or ramp survives in the dispatch path."""
    for rule in RULES:
        rule.scan()
        rule.check_bites()


def test_the_generic_admission_import_name_is_gone() -> None:
    """No shim module: a namespace dir from a test tree has no loader or origin."""
    spec = importlib.util.find_spec("admission")
    assert spec is None or (spec.origin is None and spec.loader is None)
    assert importlib.util.find_spec("crawlutron_admit") is not None


def test_the_package_owns_cgroup_sensing() -> None:
    """The cgroup stack ships inside the admission wheel, not in the crawler."""
    assert (__import__("crawlutron_admit.sensing", fromlist=["ResourceGate"]).ResourceGate) is not None
