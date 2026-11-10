"""The declared resource-lifecycle protocol: every close implementer conforms.

A class holding a resource publishes one release path under one name with one
shape; the protocol names it, and this gate keeps the implementers conforming.
"""

import ast
from typing import cast

from crawlutron.runtime.resource_lifecycle import ResourceLifecycle
from tests.chokepoint_walks import app_trees, offender_at


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
        "CrawlClient",
        "HttpCache",
        "JobStore",
        "ResourceGate",
    }, f"the close-implementer census drifted: {sorted(implementers)}"


def test_the_protocol_names_the_release_shape():
    """The protocol's own contract: one method, ``close``, awaited-able or not."""
    # __protocol_attrs__ is a runtime introspection hook outside typing's declared
    # surface; the class __dict__ lookup keeps the access honest, un-suppressed.
    members = cast("set[str]", ResourceLifecycle.__dict__["__protocol_attrs__"])
    assert members == {"close"}, f"the protocol grew beyond the release shape: {members}"


# Scope claim (#410): the gates above inspect CLASSES only; the census below pins the
# module-level release paths so a NEW one must edit this gate.

_RELEASE_VERBS = ("close", "release", "shutdown", "dispose", "teardown")
_RELEASE_CALL_ATTRS = frozenset({"close", "shutdown", "teardown", "dispose", "terminate", "aclose", "cancel"})
_RELEASE_CALL_NAMES = frozenset({"discard_if_unowned_loop"})


def _module_release_paths(trees) -> set[str]:
    """Module-level function names that release a resource, across ``trees``.

    A release VERB in the name AND a release-shaped call in the body; a
    shutdown-shaped name that releases nothing is bookkeeping, not a release path.
    """  # discard_if_unowned_loop is itself a release call (#415): a close* that
    # delegates to it is release-shaped without any ``.attr`` call.
    found: set[str] = set()
    for _path, tree in trees:
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            head = node.name.split("_")[0]
            if head not in _RELEASE_VERBS:
                continue
            if any(
                isinstance(call, ast.Call)
                and (
                    (isinstance(call.func, ast.Attribute) and call.func.attr in _RELEASE_CALL_ATTRS)
                    or (isinstance(call.func, ast.Name) and call.func.id in _RELEASE_CALL_NAMES)
                )
                for call in ast.walk(node)
            ):
                found.add(node.name)
    return found


_MODULE_RELEASE_NAMES = frozenset(
    {
        "close_async_http_client",
        "close_cffi_session",
        "close_browser_pool",
        "shutdown_doc_extraction_executor",
        "shutdown_job_runner",
        "close_queued_session",
    }
)


def test_the_module_level_release_census_is_still_the_declared_six():
    """Module-level release paths are pinned by name; a new one edits this gate.

    The class protocol does not fit shutdown wiring's heterogeneous shapes, so
    module-level paths get a census rather than a conformance gate.
    """
    found = _module_release_paths(app_trees())
    assert found == _MODULE_RELEASE_NAMES, f"the module-level release census drifted: {sorted(found)}"
