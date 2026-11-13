"""Every workflow job condition parses as the one expression it claims to be."""

from __future__ import annotations

from tests._ci_contract import SERVICE_ROOT
from tests.test_ci_enforcement import _mapping, _workflow_document


def test_every_job_condition_is_one_well_formed_expression() -> None:
    """A folded ``if:`` that tears ``${{`` from ``}}`` fails the run in zero seconds.

    GitHub reports it as a workflow-file issue and the job never runs, which reads
    here as a packaging failure that has nothing to do with packaging.
    """
    workflows = sorted(SERVICE_ROOT.joinpath(".github/workflows").glob("*.yml"))
    assert workflows, "no workflows to check: the gate would pass vacuously"
    for path in workflows:
        document = _workflow_document(path)
        for name, job in _mapping(document.get("jobs"), "jobs").items():
            condition = _mapping(job, f"job {name}").get("if")
            if not isinstance(condition, str):
                continue
            text = " ".join(condition.split())
            assert text.startswith("${{") and text.endswith("}}"), (
                f"{path.name}: job {name} has a malformed condition: {text[:80]!r}"
            )
            assert text.count("${{") == text.count("}}"), (
                f"{path.name}: job {name} has unbalanced expression braces: {text[:80]!r}"
            )
