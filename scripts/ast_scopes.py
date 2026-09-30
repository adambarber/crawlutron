"""The AST scope vocabulary shared by the walk layer and the metrics gate.

Two independent analyses of this repository need to agree on what a *scope* is.
The chokepoint walks in ``tests/chokepoint_walks.py`` decide which statement owns
a body; the duplication sweep in ``scripts/jscpd_sweep.py`` counts compound
nodes to measure where duplication first becomes visible. Neither is right on its
own -- the second is only a meaningful metric if it counts the same statements
the first walks over.

That is the whole reason these three live here rather than in either consumer.
A list of seven AST node types is exactly the kind of thing two files restate
identically and then let drift: nothing breaks when they disagree, the walks
just quietly stop covering the same ground, and the metric keeps reporting a
number. One definition, imported by both, is the only version of this that
cannot rot.
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

    The narrowed companion to :data:`SCOPE_OPENING_NODES`, for the walks that
    read ``node.body`` off the answer. Membership in the tuple and the narrowed
    type are one fact, and keeping them apart is how a walk ends up reading a
    member the declared type does not have.
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

    The predicate a walk needs rather than a bare ``isinstance``: the
    membership test and the narrowed type are the same fact, and separating them
    is how a check ends up written against a type that cannot answer it.
    """
    return isinstance(node, BODY_OWNING_NODES)
