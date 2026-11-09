"""The decline record records type names only.

Every record_declined_close call site passes type(...).__name__ as the kind,
never str(exc), so the (reason, kind) key space stays clean.
"""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import offender
from tests._walk_readers import call_nodes
from tests._walk_resolution import resolved_call_name
from tests.chokepoint_walks import app_trees
from tests.rules import Bites, Rule

_RECORD_CALL = "record_declined_close"

#: Derived by running the gate over the live tree: six call sites decline a
#: close, one per module; a seventh site must be added here deliberately.
_EXPECTED_SITES = 6


def _is_type_name(expr: ast.expr) -> bool:
    """True only for type(...).__name__: a type name, not a message.

    type(X).__name__ parses as an attribute read over a type call.
    """
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "__name__"
        and isinstance(expr.value, ast.Call)
        and isinstance(expr.value.func, ast.Name)
        and expr.value.func.id == "type"
    )


def _record_sites(tree: ast.Module) -> list[tuple[ast.Call, ast.expr | None]]:
    """Every ``record_declined_close`` call in ``tree`` with its kind argument."""
    return [
        (call, call.args[1] if len(call.args) > 1 else None)
        for call in call_nodes(tree)
        if resolved_call_name(tree, call).rsplit(".", 1)[-1] == _RECORD_CALL
    ]


def _decline_record_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every call site whose resource kind is not a type name."""
    offenders: list[str] = []
    for call, kind in _record_sites(tree):
        if kind is not None and _is_type_name(kind):
            continue  # a type name: admissible
        shape = ast.unparse(kind) if kind is not None else "no argument"
        offenders.append(
            offender(
                path,
                call.lineno,
                f"record_declined_close records {shape} as the resource kind, "
                f"not a type name (type(...).__name__)",
            )
        )
    return offenders


DECLINE_RECORD_TYPE_NAMES_ONLY = Rule(
    name="the-decline-record-records-type-names-only",
    find=_decline_record_offenders,
    summary=(
        "the decline record's resource kind is a type name at every call site; "
        "an exception message must never be recorded in its place"
    ),
    bite=lambda: (
        Bites(
            "from crawlutron.runtime.resource_lifecycle import record_declined_close\n\n"
            "record_declined_close('stream_close_failed', str(task.exception()))\n",
            "not a type name",
        ),
        Bites(
            "from crawlutron.runtime.resource_lifecycle import record_declined_close\n\n"
            "record_declined_close('harvest_failed', str(error))\n",
            "not a type name",
        ),
    ),
    clean=lambda: (
        # A type name at the call site is the whole point; a variable holding one
        # is not visible here, so call sites spell the type name directly.
        "from crawlutron.runtime.resource_lifecycle import record_declined_close\n\n"
        "record_declined_close('no_loop', type(resource).__name__)\n",
    ),
)


def test_the_decline_record_records_type_names_only():
    """Every record_declined_close call site passes a type name as the kind."""
    DECLINE_RECORD_TYPE_NAMES_ONLY.scan()


def test_the_decline_record_detector_bites():
    """The gate rejects a message in the kind position."""
    DECLINE_RECORD_TYPE_NAMES_ONLY.check_bites()


def test_the_decline_record_has_six_call_sites():
    """The six decline sites are the whole population; a new one edits this gate."""
    sites = [call for _path, tree in app_trees() for call, _kind in _record_sites(tree)]
    assert len(sites) == _EXPECTED_SITES, (
        f"the decline-record call-site count drifted: {len(sites)}, expected {_EXPECTED_SITES}"
    )
