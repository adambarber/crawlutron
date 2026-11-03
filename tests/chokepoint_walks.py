"""The walk helpers' public door; definitions live in ``tests/_walk_*.py``.

Every ``from tests.chokepoint_walks import ...`` in the tree resolves here.
"""

import ast
import pathlib

from tests._walk_assertions import (
    assert_detector,
    assert_fires_once,
    assert_stays_clean,
)
from tests._walk_membership import (
    literal_keyword_membership_offenders,
    non_member_literals,
    real_members,
)
from tests._walk_primitives import (
    APP_ROOT,
    REPO_ROOT,
    TESTS_DIR,
    TRANSPORT_DIR,
    attribute_call_on,
    enclosing_function,
    file_tree,
    lineno,
    nodes_of,
    offender,
    offender_at,
    parent_map,
    parsed,
    trees_under,
)
from tests._walk_readers import (
    assignment_targets,
    assignments,
    async_function_defs,
    attribute_calls,
    attribute_reads,
    call_nodes,
    callable_defs,
    class_defs,
    comprehensions,
    dict_displays,
    f_strings,
    from_imports,
    local_nodes,
    local_subtree,
    name_reads,
    or_nodes,
    plain_imports,
    returns,
    string_literals,
    subscript_reads,
    try_blocks,
    unannotated_writes,
    unary_ops,
    with_blocks,
    writes,
)
from tests._walk_resolution import (
    bare_callee,
    call_keyword_arguments,
    call_keyword_values,
    calls_named,
    calls_named_leaf,
    calls_to_any,
    has_method_call,
    local_call_leaf_names,
    named_calls_to_any,
    resolved_call_name,
    resolved_expr_name,
    single_target_name_assignments,
    string_argument,
)
from tests._walk_trees import (
    SANCTIONED,
    app_offenders,
    app_python_files,
    app_trees,
    functions,
    offenders_over_functions,
    relative_posix,
    trees_excluding,
)

__all__ = [
    "APP_ROOT",
    "REPO_ROOT",
    "SANCTIONED",
    "TESTS_DIR",
    "TRANSPORT_DIR",
    "app_offenders",
    "app_python_files",
    "app_trees",
    "assert_detector",
    "assert_fires_once",
    "assert_stays_clean",
    "assignment_targets",
    "assignments",
    "async_function_defs",
    "attribute_call_on",
    "attribute_calls",
    "attribute_reads",
    "bare_callee",
    "call_keyword_arguments",
    "call_keyword_values",
    "call_nodes",
    "callable_defs",
    "call_offenders",
    "calls_named",
    "calls_named_leaf",
    "calls_to_any",
    "class_defs",
    "comprehensions",
    "dict_displays",
    "enclosing_function",
    "f_strings",
    "file_tree",
    "from_imports",
    "functions",
    "has_method_call",
    "lineno",
    "literal_keyword_membership_offenders",
    "local_call_leaf_names",
    "local_nodes",
    "local_subtree",
    "name_reads",
    "named_calls_to_any",
    "nodes_of",
    "non_member_literals",
    "offender",
    "offender_at",
    "offenders_over_functions",
    "or_nodes",
    "parent_map",
    "parsed",
    "plain_imports",
    "real_members",
    "relative_posix",
    "resolved_call_name",
    "resolved_expr_name",
    "returns",
    "single_target_name_assignments",
    "stmt_offenders",
    "string_argument",
    "string_literals",
    "subscript_reads",
    "trees_excluding",
    "trees_under",
    "try_blocks",
    "unannotated_writes",
    "unary_ops",
    "with_blocks",
    "writes",
]


def call_offenders(tree: ast.Module, path: pathlib.Path, predicate, message: str) -> list[str]:
    """Offender strings for every ``ast.Call`` in ``tree`` that ``predicate`` accepts."""
    return [
        offender_at(path, node, message)
        for node in nodes_of(tree)
        if isinstance(node, ast.Call) and predicate(node)
    ]


def stmt_offenders(
    tree: ast.Module,
    path: pathlib.Path,
    outer_match,
    stmt_match,
    message: str,
) -> list[str]:
    """Offender strings for statements matching ``stmt_match`` inside nodes matching ``outer_match``."""
    out: list[str] = []
    for node in nodes_of(tree):
        if not outer_match(node):
            continue
        for stmt in ast.walk(node):
            if stmt_match(stmt):
                out.append(offender_at(path, stmt, message))
    return out
