"""Result error fields carry declared shapes, never raw exception text."""

from __future__ import annotations

import ast
import pathlib

from tests._error_door_trees import error_door_trees
from tests.chokepoint_walks import (
    SANCTIONED,
    calls_named_leaf,
    dict_displays,
    named_calls_to_any,
    offender_at,
    resolved_expr_name,
)
from tests.rules import Bites, Registry, Rule, Trips

# --------------------------------------------------------------------------- Error-envelope shape
ENVELOPE_SANCTIONED = SANCTIONED | {"exceptions.py", "error_rendering.py"}  # the serializer home


def _dict_keys(node: ast.Dict) -> set[str]:
    return {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _envelope_dict_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag hand-rolled error envelopes; the factory is the only definition: any bare
    ``HTTPException(...)`` (laundered into a fabricated code), the pre-factory
    ``HTTPException(detail={...})`` literal, and any dict with all three keys.
    """
    offenders = [
        offender_at(
            path,
            call,
            "HTTPException detail= envelope literal"
            if any(
                kw.arg == "detail"
                and isinstance(kw.value, ast.Dict)
                and ({"code", "message"} <= _dict_keys(kw.value))
                for kw in call.keywords
            )
            else "bare HTTPException construction",
        )
        for call in calls_named_leaf(tree, {"HTTPException"})
    ]
    offenders.extend(
        offender_at(path, node, "error envelope dict literal")
        for node in dict_displays(tree)
        if {"code", "message", "details"} <= _dict_keys(node)
    )
    return offenders


_HAND_ROLLED_ERROR_ENVELOPES_FORBIDDEN = Rule(
    name="no-hand-rolled-error-envelopes",
    find=_envelope_dict_offenders,
    summary="Use the shared render_error/http_error, never a hand-built envelope",
    excludes=ENVELOPE_SANCTIONED,
    scope=error_door_trees,
    bite=lambda: (
        # Four offenders from one source: two bare HTTPException builds, one detail= envelope literal, one
        # three-key dict.
        Trips(
            "raise HTTPException(status_code=404, detail={'code': 'not_found', 'message': 'gone'})\n"
            "response = {'code': 'pending', 'message': 'm', 'details': {}}\n"
            "raise HTTPException(status_code=404, detail='plain string')\n"
            "raise fastapi.HTTPException(status_code=500)\n",
            count=4,
        ),
    ),
    clean=lambda: (
        "from crawlutron_rest.api.exceptions import http_error\n"
        "raise http_error(404, 'not_found', 'gone')\n"
        "response = build_thing({'code': 1, 'message': 'x'})\n",
    ),
)


# --------------------------------------------------------------------------- Raw exception text never

# owners: build/classify the terminal sinks themselves
_TERMINAL_RESULT_SANCTIONED = frozenset({"results.py", "classification.py"})


_RESULT_ERROR_SINKS = frozenset({"CrawlResult", "terminal_crawl_result"})
_TOOL_ERROR_SINKS = frozenset({"ToolResult"})


_EXCEPTION_NAMES = frozenset({"error", "exc", "exception", "err"})
_SANCTIONED_TOOL_TEXT_ARG = "rendered.message"  # resolved renderer message; never a name allowlist


def _is_raw_exception_text(value: ast.expr, tree: ast.AST, *, named_arg: bool = False) -> bool:
    """Flag raw exception text; ToolResult.text admits only the renderer's str(rendered.message)."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("str", "repr"):
        if not named_arg:
            return True
        if not value.args:
            return True
        return resolved_expr_name(tree, value.args[0]) != _SANCTIONED_TOOL_TEXT_ARG
    if isinstance(value, ast.JoinedStr):
        return any(
            isinstance(part, ast.FormattedValue)
            and isinstance(part.value, ast.Name)
            and part.value.id in _EXCEPTION_NAMES
            for part in value.values
        )
    return False


def _raw_wire_text_offenders(
    tree: ast.AST, path: pathlib.Path, sinks: frozenset[str], field: str, *, named_arg: bool = False
) -> list[str]:
    """Flag a wire-visible result built with the raw text of an exception."""
    offenders: list[str] = []
    for node, callee in named_calls_to_any(tree, sinks):
        for kw in node.keywords:
            if kw.arg == field and _is_raw_exception_text(kw.value, tree, named_arg=named_arg):
                offenders.append(offender_at(path, node, f"{callee}({field}=<raw exception text>)"))
    return offenders


NO_RAW_EXCEPTION_TEXT_IN_RESULT_ERROR_FIELD = Rule(
    name="no-raw-exception-text-in-result-error-field",
    find=_raw_wire_text_offenders,
    kwargs_for=lambda relative: {"sinks": _RESULT_ERROR_SINKS, "field": "error"},
    summary="classify via classify_error_code, never raw exception text",
    excludes=SANCTIONED | _TERMINAL_RESULT_SANCTIONED,
    scope=error_door_trees,
    bite=lambda: (
        # ``str(exc)`` on a direct result construction, and the same through the sanctioned primitive -- both
        # sinks carry the raw text verbatim.
        Bites("def f(exc):\n    return CrawlResult(error=str(exc))\n", "raw exception text"),
        Bites("def f(exc):\n    return terminal_crawl_result(error=str(exc))\n", "raw exception text"),
        # An f-string interpolating the exception is the leak in disguise.
        Bites("def f(error):\n    return CrawlResult(error=f'failed: {error}')\n", "raw exception text"),
        Bites("def f(e):\n    return CrawlResult(error=repr(e))\n", "raw exception text"),
    ),
    clean=lambda: (
        # The classified shape: an already-classified error reaches the wire.
        "def f(exc):\n    return terminal_crawl_result(error=classify_error_code(exc))\n",
    ),
)


NO_RAW_EXCEPTION_TEXT_IN_TOOL_RESULT = Rule(
    name="no-raw-exception-text-in-tool-result",
    find=_raw_wire_text_offenders,
    kwargs_for=lambda relative: {"sinks": _TOOL_ERROR_SINKS, "field": "text", "named_arg": True},
    summary="route ValueError through the renderer, never raw str(error) in ToolResult.text",
    excludes=SANCTIONED,
    scope=error_door_trees,
    bite=lambda: (
        Bites(
            "def f(error):\n    return ToolResult(text=str(error), is_error=True)\n",
            "raw exception text",
        ),
        Bites(
            "def f(cause):\n    return ToolResult(text=str(cause), is_error=True)\n",
            "raw exception text",
        ),
        Bites(
            "def f(other):\n    return ToolResult(text=str(other.message), is_error=True)\n",
            "raw exception text",
        ),
    ),
    clean=lambda: (
        "def f(rendered):\n    return ToolResult(text=str(rendered.message), is_error=True)\n",
        "def g(error):\n"
        "    return tool_error_result(ValidationRefusal('validation_error', validation_message(error)))\n",
    ),
)


RULES = Registry(
    (
        _HAND_ROLLED_ERROR_ENVELOPES_FORBIDDEN,
        NO_RAW_EXCEPTION_TEXT_IN_RESULT_ERROR_FIELD,
        NO_RAW_EXCEPTION_TEXT_IN_TOOL_RESULT,
    )
)


def test_no_rule_violations():
    """Every HTTP error body flows through error_envelope/http_error."""
    RULES.check_all_clean()


def test_rules_have_working_bites():
    """Each declared bite must fire, or its rule enforces nothing."""
    RULES.check_all_bite()


# Known failures classify to named rows, never the network_error fallback
def test_known_storage_refusals_classify_not_network_error() -> None:
    """ReadRejected and WriteRejected are knowable storage failures; they must not
    degrade to the generic network_error fallback."""
    from crawlutron.models.taxonomy import Classification
    from crawlutron.storage.safe_db import ReadRejected, WriteRejected
    from crawlutron.transport.classification import classify_error_code

    for exc in (ReadRejected("boom"), WriteRejected("boom")):
        classified = classify_error_code(exc)
        assert classified == Classification.connection_error
        assert classified != Classification.network_error
    # A genuinely unknowable exception still falls back to network_error.
    assert classify_error_code(RuntimeError("boom")) == Classification.network_error
