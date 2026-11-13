"""The release names its version from the CI run that published it."""

from __future__ import annotations

import yaml

from tests._ci_contract import SERVICE_ROOT
from tests._ci_contract import job_steps as _job_steps

RELEASE_WORKFLOW_PATH = SERVICE_ROOT / ".github" / "workflows" / "release.yml"


def test_the_release_reads_its_tag_from_the_ci_run() -> None:
    """A `workflow_run`'s GITHUB_REF_NAME is the default branch, not the tag.

    The tag lives on `github.event.workflow_run.head_branch`; reading the workflow
    ref there names every artifact after `main`.
    """
    steps = _job_steps(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"), "distributions")
    script = "\n".join(str(step.get("run", "")) for step in steps)
    assert "GITHUB_REF_NAME" not in script, (
        "a workflow_run's GITHUB_REF_NAME is the default branch, not the release tag"
    )
    assert "github.event.workflow_run.head_branch" in script, (
        "the release must name its version from the CI run's head branch, which is the tag"
    )


def test_the_release_workflow_is_this_parseable_document() -> None:
    """The workflow parses, so the ref check above reads a real job graph."""
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    assert "distributions" in document["jobs"]
