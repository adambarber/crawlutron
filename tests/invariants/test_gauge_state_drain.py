"""GaugeState was exported but never produced or consumed: dead surface.
_ResourceReading's booleans are the sole sensing vocabulary; a reintroduced
health enum would disagree with them, so this rule refuses it.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_paths import ADMISSION_ROOT
from tests.chokepoint_walks import offender_at, trees_under
from tests.rules import Bites, Registry, Rule

_DEAD_NAME = "GaugeState"
_RETIRED = (
    f"declares or re-exports {_DEAD_NAME} without a producer or consumer: "
    "the sensing vocabulary is _ResourceReading's booleans"
)

_BITE = f"from other import {_DEAD_NAME}\n\nclass {_DEAD_NAME}:\n    pass\n\n__all__ = ['{_DEAD_NAME}']\n"


def _gauge_state_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Name every definition, import or export of the retired GaugeState."""
    offenders: list[str] = []
    for node in tree.body:
        if (
            isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == _DEAD_NAME
        ):
            offenders.append(offender_at(path, node, _RETIRED))
        elif isinstance(node, ast.ImportFrom) and any(a.name == _DEAD_NAME for a in node.names):
            offenders.append(offender_at(path, node, _RETIRED))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                continue
            names = node.value
            if isinstance(names, (ast.List, ast.Tuple, ast.Set)) and any(
                isinstance(e, ast.Constant) and e.value == _DEAD_NAME for e in names.elts
            ):
                offenders.append(offender_at(path, node, _RETIRED))
    return offenders


GAUGE_STATE_IS_RETIRED = Rule(
    name="gauge-state-is-retired",
    find=_gauge_state_offenders,
    summary=_RETIRED,
    scope=lambda *, exclude: trees_under(ADMISSION_ROOT),
    bite=lambda: Bites(_BITE, marker=_DEAD_NAME, count=3),
    clean=lambda: (
        "class Health:\n    pass\n\n__all__ = ['Health']\n",
        "from crawlutron_admit.gauge import ResourceGauge\n\n__all__ = ['ResourceGauge']\n",
    ),
)

RULES = Registry((GAUGE_STATE_IS_RETIRED,))


def test_no_rule_violations():
    """The dead enum and its re-export are absent from the admit package."""
    RULES.check_all_clean()
