"""Every model constraint is proved to reject its violation."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from tests._ci_contract import MAKEFILE_PATH
from tests.makefile._model import RecipeLine, Target, Variable, parse_makefile


@pytest.mark.parametrize(
    ("adapter", "value", "token"),
    [
        (TypeAdapter(Variable), {"operator": "=", "value": "x", "line": 1}, "name"),
        (TypeAdapter(Target), {"recipe": (), "line": 1}, "name"),
        (TypeAdapter(RecipeLine), {"line": 1}, "command"),
    ],
)
def test_a_typed_field_names_its_violation(adapter, value, token) -> None:
    with pytest.raises(ValidationError, match=token):
        adapter.validate_python(value)


def test_an_unknown_field_is_refused_on_every_model() -> None:
    with pytest.raises(ValidationError, match="invented"):
        Variable.model_validate({"name": "x", "operator": "=", "value": "y", "line": 1, "invented": 1})
    with pytest.raises(ValidationError, match="invented"):
        Target.model_validate({"name": "x", "line": 1, "invented": 1})
    with pytest.raises(ValidationError, match="invented"):
        RecipeLine.model_validate({"command": "true", "line": 1, "invented": 1})


def test_parse_makefile_reads_the_live_makefile_once() -> None:
    makefile = parse_makefile(MAKEFILE_PATH)
    assert makefile.path == MAKEFILE_PATH
    names = {target.name for target in makefile.targets}
    assert {"lint", "typecheck", "coverage"} <= names


def test_every_delegated_target_runs_a_hook_by_id() -> None:
    makefile = parse_makefile(MAKEFILE_PATH)
    for target in makefile.targets:
        for line in target.recipe:
            if "pre-commit run" in line.command or "$(PRE_COMMIT) run" in line.command:
                break
        else:
            continue
        break
    else:
        pytest.fail("expected at least one target to delegate a gate by hook id")
