"""Bite: missing member dep.

A pin on a dependency the version registry does not carry is refused with the
exact `unregistered dependency` diagnostic, naming the file and line.
"""

from __future__ import annotations

from tests.release_bites.conftest import PinTree

WORKFLOW_NAME = "pvtest_member_dep.yml"
DEPENDENCY = "owner/unregistered-dep"
SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def test_missing_member_dep_refusal_message(pin_tree: PinTree):
    """pin_verify check names the unregistered dependency as its sole error."""
    pin_tree.add_workflow(
        WORKFLOW_NAME,
        "name: pv\non: push\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
        f"    steps:\n      - uses: {DEPENDENCY}@{SHA}\n",
    )
    result = pin_tree.check()
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        f".github/workflows/{WORKFLOW_NAME}:7: unregistered dependency {DEPENDENCY}"
    ]
