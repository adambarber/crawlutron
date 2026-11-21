"""Packaging tests: every commit proves packaging; only a tag may publish."""

from __future__ import annotations

from tests._ci_contract import PACKAGING_WORKFLOW_PATH, triggers, workflow_document


def test_packaging_runs_only_after_a_green_ci_run() -> None:
    """Packaging is downstream of the gates, so a red commit never reaches it.

    Running the two in parallel makes a failure ambiguous: a packaging break and a
    gate break look the same, and a red commit is judged on a build it never cleared.
    """
    assert PACKAGING_WORKFLOW_PATH.is_file(), "a packaging workflow must exist"
    document = workflow_document(PACKAGING_WORKFLOW_PATH)
    assert "workflow_call" in triggers(document), "packaging is a reusable workflow"
    assert "push" not in triggers(document), "packaging must not run on the push itself"
    assert "workflow_run" not in triggers(document), "packaging must not use workflow_run"


def test_packaging_workflow_publishes_nothing() -> None:
    """Every commit proves the packaging; only a tag may publish.

    A packaging workflow that could upload would make "released" and "merged"
    the same event, which is exactly what the tag gate exists to prevent.
    """
    import yaml

    assert PACKAGING_WORKFLOW_PATH.is_file(), "a packaging workflow must exist"
    workflow = PACKAGING_WORKFLOW_PATH.read_text(encoding="utf-8")
    document = yaml.safe_load(workflow)
    assert isinstance(document, dict)
    assert document.get("permissions") == {"contents": "read"}, (
        "the packaging workflow needs only its read scope"
    )
    for banned in ("pypa/gh-action-pypi-publish", "twine upload", "docker push", "push: true"):
        assert banned not in workflow, f"the packaging workflow must not publish ({banned!r})"
    assert "twine check --strict" in workflow, "the packaging workflow must inspect every artifact"
    assert "scripts.check_packaging" in workflow, "every wheel must install and import"
