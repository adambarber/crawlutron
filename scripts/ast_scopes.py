"""The AST scope vocabulary shared by the walk layer and the metrics gate.

Two independent analyses must agree on what a *scope* is (the chokepoint walks, the
duplication sweep); one definition imported by both is the only version that cannot rot.
"""

from __future__ import annotations

import ast
from typing import TypeGuard

#: Statements that open a nested scope for walks: ``ast.ExceptHandler`` owns a
#: real suite (a definition inside ``except:`` is legal Python).
SCOPE_OPENING_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
)

#: Statements that own a body -- what "the first statement of a block" asks for.
#: ``ast.stmt`` declares no ``body``; this set is scope-openers plus definitions.
BODY_OWNING_NODES: tuple[type[ast.AST], ...] = (
    ast.Module,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
) + SCOPE_OPENING_NODES


def is_scope_opener(
    node: ast.AST,
) -> TypeGuard[
    ast.If | ast.For | ast.AsyncFor | ast.While | ast.With | ast.AsyncWith | ast.Try | ast.ExceptHandler
]:
    """Whether ``node`` is a scope-opening node, narrowed to the kinds that are.

    The narrowed companion to :data:`SCOPE_OPENING_NODES`, for walks that read
    ``node.body`` off the answer.
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
    | ast.ExceptHandler
]:
    """Whether ``node`` is a node that owns a body, narrowed to the kinds that do.

    The predicate a walk needs rather than a bare ``isinstance``; each kind is named
    because ``ast.stmt`` declares no ``body``.
    """
    return isinstance(node, BODY_OWNING_NODES)
