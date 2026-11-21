"""Bite: image version 0.0.0.

A pin whose tag comment drifts from the registry version is refused with the
exact `registry says` diagnostic, naming the file and line.
"""

from __future__ import annotations

from tests.release_bites.conftest import PinTree

WORKFLOW_NAME = "pvtest_version.yml"
DEPENDENCY = "actions/checkout"


def test_image_version_0_0_0_refusal_message(pin_tree: PinTree):
    """pin_verify check names the registry version for a drifting pin comment."""
    sha, version = pin_tree.pin(DEPENDENCY)
    pin_tree.add_workflow(
        WORKFLOW_NAME,
        "name: pv\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        f"    steps:\n      - uses: {DEPENDENCY}@{sha}  # v0.0.0\n",
    )
    result = pin_tree.check()
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        f".github/workflows/{WORKFLOW_NAME}:7: {DEPENDENCY} declares '0.0.0', registry says {version!r}"
    ]
