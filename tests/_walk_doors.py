"""Shared door-walk primitives for the read and write gates.

One home for conn_callee, sql_operand, inside_door; it stops at nested
def/class/lambda boundaries so door-defined reads read as bare.
"""

from __future__ import annotations

import ast

from tests._walk_resolution import resolved_call_name


def conn_callee(call: ast.Call, methods: frozenset[str]) -> ast.Attribute | None:
    """``call.func`` when it runs one of ``methods`` on ``conn`` or any ``... ._conn``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in methods:
        return None
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id == "conn":
        return func
    if isinstance(receiver, ast.Attribute) and receiver.attr == "_conn":
        return func
    return None


def _fstring_literal_prefix(node: ast.JoinedStr) -> str | None:
    """Constant text before the first f-string substitution, or None if undecidable."""
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            break
    return "".join(parts) if parts else None


def sql_operand(call: ast.Call, tree: ast.Module | None = None) -> str | None:
    """Decidable SQL for call's first argument, or None when opaque.

    Literal, f-string literal prefix, or module-level string-constant name.
    Function-local constants and attribute/parameter operands are opaque.
    """
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr):
        return _fstring_literal_prefix(arg)
    if not isinstance(arg, ast.Name) or tree is None:
        return None
    # Module-level string-constant assignments only; function locals are opaque.
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or target.id != arg.id:
            continue
        val = stmt.value
        if isinstance(val, ast.Constant) and isinstance(val.value, str):
            return val.value
    return None


def inside_door(
    call: ast.Call,
    tree: ast.Module,
    parents: dict[int, ast.AST],
    door_names: frozenset[str],
) -> bool:
    """True when ``call`` sits lexically inside a ``with <door>(...)`` block.

    Walks up to the module, stopping at nested def/class/lambda boundaries; a
    door containing a ``def`` does not cover reads defined inside it.
    """
    node: ast.AST | None = call
    while node is not None:
        parent = parents.get(id(node))
        if isinstance(parent, ast.With):
            for item in parent.items:
                ctx = item.context_expr
                if (
                    isinstance(ctx, ast.Call)
                    and resolved_call_name(tree, ctx).rsplit(".", 1)[-1] in door_names
                ):
                    return True
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return False
        node = parent
    return False
