"""The workflow security surface: pinned actions, narrow tokens, no event text
spliced into a shell, and no publish before a smoke."""

from __future__ import annotations

import re

import pytest
import yaml

from tests._ci_contract import (
    PACKAGING_WORKFLOW_PATH,
    RELEASE_WORKFLOW_PATH,
    SERVICE_ROOT,
    WORKFLOW_PATH,
    _job_run_steps,
)


def test_every_action_is_sha_pinned_and_permissions_are_declared() -> None:
    """#624: mutable tags (`@v4`) are repointable; every `uses:` must pin a
    full-length commit SHA, and the workflow must declare its permissions so
    the default write-all token scope cannot leak into a compromised step.
    """
    for workflow_path in (WORKFLOW_PATH, RELEASE_WORKFLOW_PATH, PACKAGING_WORKFLOW_PATH):
        document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)

        def walk(value: object, file_name: str = workflow_path.name) -> None:
            if isinstance(value, dict):
                uses = value.get("uses")
                if isinstance(uses, str) and "@" in uses:
                    ref = uses.rsplit("@", 1)[1]
                    assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                        f"action ref {uses!r} in {file_name} is not a full-length commit SHA"
                    )
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(document.get("jobs"))
    ci = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(ci, dict)
    permissions = ci.get("permissions")
    assert permissions == {"contents": "read", "pull-requests": "read"}, (
        f"CI must declare exactly its two read scopes, found {permissions!r}"
    )


def _assert_no_event_splicing(workflow: str, label: str) -> None:
    """An event field is attacker-shaped text: it reaches a shell through `env:`."""
    for name, run in _job_run_steps(workflow):
        assert "${{ github.event" not in run, (
            f"{label} job {name} splices event input into its script; bind it through env"
        )


def test_no_run_step_splices_event_input_into_shell() -> None:
    """zizmor template-injection: a branch named like a tag must never become code."""
    for path in (WORKFLOW_PATH, RELEASE_WORKFLOW_PATH, PACKAGING_WORKFLOW_PATH):
        _assert_no_event_splicing(path.read_text(encoding="utf-8"), path.name)


def test_a_run_step_that_splices_an_event_field_is_caught() -> None:
    """Bites: the old inline branch interpolation fails the check."""
    workflow = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")
    marker = "python3 scripts/release/emit_roster.py"
    broken = workflow.replace(marker, f'{marker} "${{{{ github.event.workflow_run.head_branch }}}}"', 1)
    assert broken != workflow, "the injection did not apply; the probe proves nothing"
    with pytest.raises(AssertionError):
        _assert_no_event_splicing(broken, "mutated release")


def _publishes(step: dict[str, object]) -> bool:
    """True when this step hands the image to the registry."""
    with_block = step.get("with")
    return "docker push" in str(step.get("run", "")) or (
        isinstance(with_block, dict) and with_block.get("push") is True
    )


def test_the_published_graph_is_probed_before_the_release_lands() -> None:
    """#1170: a partial publish left every pip install of the root failing."""
    document = yaml.safe_load(RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    assert "install-graph" in jobs, "the release must probe the published install graph"
    assert "publish" in jobs["install-graph"]["needs"], (
        "the install probe must wait for the package to be published"
    )


def test_the_image_is_pushed_only_after_it_smokes() -> None:
    """A tag that failed its smoke must never exist: :0.4.0rc1 was pushed dead."""
    # The image job calls a script that handles smoke, push, and attest in order
    image_script = (SERVICE_ROOT / "scripts" / "release" / "image.py").read_text()
    # Verify the script does smoke before push
    smoke_idx = image_script.find("_smoke_image")
    push_idx = image_script.find("_push_and_attest")
    assert smoke_idx != -1, "script must have smoke function"
    assert push_idx != -1, "script must have push function"
    assert smoke_idx < push_idx, "smoke must come before push in script"
    # Verify the main function calls them in order
    main_body = image_script[image_script.find("def main") :]
    assert main_body.find("_smoke_image") < main_body.find("_push_and_attest")
