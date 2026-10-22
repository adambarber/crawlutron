"""The AST scope vocabulary shared by the walk layer and the metrics gate.

Two independent analyses must agree on what a *scope* is (the chokepoint walks, the
duplication sweep); one definition imported by both is the only version that cannot rot.
"""

from __future__ import annotations

import ast
from typing import TypeGuard

#: The statements that open a nested scope. Beyond the classic compound
#: statements, ``ast.ExceptHandler`` owns a real suite a walk can treat as a
#: block (a definition inside ``except:`` is legal Python). ``ast.Match`` is
#: deliberately NOT here: it owns ``.cases``, not ``.body``, so walks that read
#: ``node.body`` would break on it, and the metrics budgets are measured without
#: it. This is the vocabulary for "can contain a definition", and every consumer
#: imports it rather than restating it: two lists that happen to agree are two
#: lists that can stop agreeing (tests/test_ast_scopes.py holds that line).
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


def is_scope_opener(node: ast.AST) -> TypeGuard[ast.stmt | ast.ExceptHandler]:
    """Whether ``node`` is a scope-opening node, narrowed to the kinds that are.

    The narrowed companion to :data:`SCOPE_OPENING_NODES`, for walks that read
    ``node.body`` off the answer.
    """
    return isinstance(node, SCOPE_OPENING_NODES)


def is_body_owner(node: ast.AST) -> TypeGuard[ast.Module | ast.stmt | ast.ExceptHandler]:
    """Whether ``node`` is a node that owns a body, narrowed to the kinds that do.

    The predicate a walk needs rather than a bare ``isinstance`` -- membership test and
    narrowed type are the same fact, and separating them lets a check miss the type.
    """
    return isinstance(node, BODY_OWNING_NODES)
