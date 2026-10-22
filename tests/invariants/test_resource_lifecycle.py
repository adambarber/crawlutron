"""The declared resource-lifecycle protocol: every close implementer conforms.

A class holding a resource publishes one release path under one name with one
shape; the protocol names it, and this gate keeps the implementers conforming.
"""

import ast

from app.runtime.resource_lifecycle import ResourceLifecycle
from tests.chokepoint_walks import REPO_ROOT, app_trees, offender_at


def _close_implementers(tree: ast.Module, path) -> list[ast.ClassDef]:
    """App classes that define ``close`` (sync or async) on the class itself,
    except a Protocol, whose ``close`` is a declaration, not a release path."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and not any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
        and any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "close"
            for item in node.body
        )
    ]


def _conforms(node: ast.ClassDef) -> bool:
    """Whether the class's ``close`` has the protocol's shape: an argument list of
    just self, and a return that a caller may await or ignore."""
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "close":
            args = [a.arg for a in item.args.args]
            return args == ["self"] and item.returns is not None
    return False


def test_every_close_implementer_conforms_to_the_lifecycle_protocol():
    """Eight classes release resources; the protocol is the one shape they all
    speak, so a drifted signature (extra args, untyped return) is a finding."""
    offenders = [
        offender_at(path, node, f"{node.name}.close does not conform to ResourceLifecycle")
        for path, tree in app_trees()
        for node in _close_implementers(tree, path)
        if not _conforms(node)
    ]
    assert not offenders, "close implementers drifted:\n  " + "\n  ".join(offenders)


def test_the_eight_declared_implementers_are_still_the_eight():
    """The protocol's census: the tree's public close implementers are the eight
    the issue names, so a new resource class cannot grow a private release path
    without this gate seeing it."""
    implementers = {
        node.name
        for _path, tree in app_trees()
        for node in _close_implementers(tree, _path)
        if not node.name.startswith("_")
    }
    assert implementers == {
        "BrowserDriver",
        "BrowserPool",
        "BrowserRenderer",
        "CoordinationStore",
        "HttpCache",
        "JobStore",
        "ResourceGate",
        "ResultSpill",
    }, f"the close-implementer census drifted: {sorted(implementers)}"


def test_the_protocol_bites_on_a_drifted_close():
    """The declared shape is load-bearing: a close with extra arguments or no
    declared return is not the protocol, and the gate names it."""
    drifted = ast.parse(
        "class Drifted:\n    def close(self, force: bool) -> None: ...\n    async def untyped(self): ...\n"
    )
    bad = [
        node.name
        for node in _close_implementers(drifted, REPO_ROOT / "app" / "_synthetic.py")
        if not _conforms(node)
    ]
    assert bad == ["Drifted"], "the conformance check stopped firing"


def test_the_protocol_names_the_release_shape():
    """The protocol's own contract: one method, ``close``, awaited-able or not --
    the SpillSink spelling the tree already proved out."""
    members = ResourceLifecycle.__protocol_attrs__  # type: ignore[attr-defined]
    assert members == {"close"}, f"the protocol grew beyond the release shape: {members}"
