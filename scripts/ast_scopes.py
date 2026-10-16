"""The AST scope vocabulary shared by the walk layer and the metrics gate.

Two independent analyses must agree on what a *scope* is (the chokepoint walks, the
duplication sweep); one definition imported by both is the only version that cannot rot.
"""

from __future__ import annotations

import ast
from typing import TypeGuard

#: The statements that open a nested scope -- the compound statements. This is
#: the vocabulary for "can contain a definition", and the metrics gate imports it
#: rather than restating it: two lists that happen to agree on these seven are
#: two lists that can stop agreeing.
SCOPE_OPENING_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)

#: A statement that owns a body -- a suite of statements. A walk asking "is this
#: the first statement of a block" is asking for exactly this, and ``ast.stmt``
#: -- the union every statement belongs to -- declares no ``body`` on any of
#: them, so the question could not be asked against it.
#:
#: Built as the scope-opening set plus the definition-bearing statements rather
#: than as a list that happens to agree with it.
BODY_OWNING_NODES: tuple[type[ast.AST], ...] = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
) + SCOPE_OPENING_NODES


def is_scope_opener(
    node: ast.AST,
) -> TypeGuard[ast.If | ast.For | ast.AsyncFor | ast.While | ast.With | ast.AsyncWith | ast.Try]:
    """Whether ``node`` is a compound statement, narrowed to the kinds that are.

    The narrowed companion to :data:`SCOPE_OPENING_NODES`, for walks that read
    ``node.body`` off the answer -- membership and type are one fact, kept together.
    """
    return isinstance(node, SCOPE_OPENING_NODES)


def is_body_owner(
    node: ast.AST,
) -> TypeGuard[
    ast.Module
    | ast.FunctionDef
    | ast.AsyncFunctionDef
    | ast.ClassDef
    | ast.If
    | ast.For
    | ast.AsyncFor
    | ast.While
    | ast.With
    | ast.AsyncWith
    | ast.Try
]:
    """Whether ``node`` is a statement that owns a body, narrowed to the kinds that do.

    The predicate a walk needs rather than a bare ``isinstance`` -- membership test and
    narrowed type are the same fact, and separating them lets a check miss the type.
    """
    return isinstance(node, BODY_OWNING_NODES)
