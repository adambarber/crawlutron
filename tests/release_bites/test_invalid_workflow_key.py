"""Bite: invalid workflow key.

A pin that is not a commit SHA is refused with the exact `is not a commit SHA`
diagnostic, naming the file and line that carries it.
"""

from __future__ import annotations

from tests.release_bites.conftest import PinTree

WORKFLOW_NAME = "pvtest_invalid_key.yml"
DEPENDENCY = "actions/checkout"


def test_invalid_workflow_key_refusal_message(pin_tree: PinTree):
    """pin_verify check names the bad pin as its sole error."""
    _, version = pin_tree.pin(DEPENDENCY)
    pin_tree.add_workflow(
        WORKFLOW_NAME,
        "name: pv\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        f"    steps:\n      - uses: {DEPENDENCY}@not-a-sha  # v{version}\n",
    )
    result = pin_tree.check()
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        f".github/workflows/{WORKFLOW_NAME}:7: {DEPENDENCY} pin 'not-a-sha' is not a commit SHA"
    ]
