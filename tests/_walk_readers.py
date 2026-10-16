"""Call, assignment and function readers over one tree."""

import ast
from collections.abc import Iterable, Iterator
from functools import cache

from tests._walk_primitives import _narrow, nodes_of


# The narrow walks below are a detector's vocabulary, named for what a rule asks of the tree rather than for
# the ast class that happens to answer it.
def call_nodes(root: ast.AST) -> tuple[ast.Call, ...]:
    """Every call in `root`, in walk order. The busiest walk in the suite."""
    return tuple(node for node in _narrow(root, "call", ast.Call) if isinstance(node, ast.Call))


def or_nodes(root: ast.AST) -> tuple[ast.BoolOp, ...]:
    """Every two-valued ``a or b``, in walk order.

    "coerces a possibly-absent value into a present one" is spelled ``x or y``
    in Python; the three-line guard is one clone per rule that wants it."""
    return tuple(
        node
        for node in _narrow(root, "boolop", ast.BoolOp)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2
    )


def attribute_reads(root: ast.AST) -> tuple[ast.Attribute, ...]:
    """Every ``obj.attr`` read. What a rule about member access walks."""
    return tuple(
        node for node in _narrow(root, "attribute", ast.Attribute) if isinstance(node, ast.Attribute)
    )


def name_reads(root: ast.AST) -> tuple[ast.Name, ...]:
    """Every bare name reference. What a rule about identifier use walks."""
    return tuple(node for node in _narrow(root, "name", ast.Name) if isinstance(node, ast.Name))


def string_literals(root: ast.AST) -> tuple[ast.Constant, ...]:
    """Every ``ast.Constant``: string, number, bool and ``None`` alike."""
    return tuple(node for node in _narrow(root, "constant", ast.Constant) if isinstance(node, ast.Constant))


def subscript_reads(root: ast.AST) -> tuple[ast.Subscript, ...]:
    """Every ``a[b]``. Indexed access, which attribute reads do not see."""
    return tuple(
        node for node in _narrow(root, "subscript", ast.Subscript) if isinstance(node, ast.Subscript)
    )


def dict_displays(root: ast.AST) -> tuple[ast.Dict, ...]:
    """Every ``{...}`` dict display, as opposed to a comprehension."""
    return tuple(node for node in _narrow(root, "dict", ast.Dict) if isinstance(node, ast.Dict))


def class_defs(root: ast.AST) -> tuple[ast.ClassDef, ...]:
    """Every class definition."""
    return tuple(node for node in _narrow(root, "class", ast.ClassDef) if isinstance(node, ast.ClassDef))


def try_blocks(root: ast.AST) -> tuple[ast.Try, ...]:
    """Every ``try`` statement -- not ``TryStar``, whose handlers differ."""
    return tuple(node for node in _narrow(root, "try", ast.Try) if isinstance(node, ast.Try))


def returns(root: ast.AST) -> tuple[ast.Return, ...]:
    """Every ``return``. What a rule about a function's exit walks."""
    return tuple(node for node in _narrow(root, "return", ast.Return) if isinstance(node, ast.Return))


def unary_ops(root: ast.AST) -> tuple[ast.UnaryOp, ...]:
    """Every unary expression: ``not x``, ``-x``, ``~x``."""
    return tuple(node for node in _narrow(root, "unary", ast.UnaryOp) if isinstance(node, ast.UnaryOp))


def f_strings(root: ast.AST) -> tuple[ast.JoinedStr, ...]:
    """Every f-string, as one node however many holes it interpolates."""
    return tuple(node for node in _narrow(root, "fstring", ast.JoinedStr) if isinstance(node, ast.JoinedStr))


def callable_defs(root: ast.AST) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda, ...]:
    """Everything a rule can call by name: both def forms and lambdas."""
    return tuple(
        node
        for node in _narrow(root, "callable", ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    )


def writes(root: ast.AST) -> tuple[ast.Assign | ast.AnnAssign | ast.AugAssign, ...]:
    """Every statement that binds a name. `assignments` turns these into triples."""
    return tuple(
        node
        for node in _narrow(root, "write", ast.Assign, ast.AnnAssign, ast.AugAssign)
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign)
    )


def assignment_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    """The target expressions of a write, whatever form the write took.

    ``Assign`` carries a list, ``AnnAssign`` a single node; the four-line
    branch was copied into six detectors, one of which read a tuple silently."""
    return list(node.targets) if isinstance(node, ast.Assign) else [node.target]


def plain_imports(root: ast.AST) -> tuple[ast.Import, ...]:
    """``import x`` only, split from the from-form because the module a tree
    pulls in is the first part of the alias here, not ``node.module``."""
    return tuple(node for node in _narrow(root, "plain-import", ast.Import) if isinstance(node, ast.Import))


def from_imports(root: ast.AST) -> tuple[ast.ImportFrom, ...]:
    """``from x import y`` only, the other half of the split above."""
    return tuple(
        node for node in _narrow(root, "from-import", ast.ImportFrom) if isinstance(node, ast.ImportFrom)
    )


def unannotated_writes(root: ast.AST) -> tuple[ast.Assign | ast.AugAssign, ...]:
    """Writes that bind without declaring a type: ``=`` and ``+=``.

    ``assignments`` minus the ``ast.AnnAssign`` form, for a rule about
    rebinding an existing name."""
    return tuple(
        node
        for node in _narrow(root, "unannotated-write", ast.Assign, ast.AugAssign)
        if isinstance(node, ast.Assign | ast.AugAssign)
    )


def async_function_defs(root: ast.AST) -> tuple[ast.AsyncFunctionDef, ...]:
    """Only the ``async def`` half of what `functions` returns together."""
    return tuple(
        node
        for node in _narrow(root, "async-function", ast.AsyncFunctionDef)
        if isinstance(node, ast.AsyncFunctionDef)
    )


def with_blocks(root: ast.AST) -> tuple[ast.With | ast.AsyncWith, ...]:
    """Every ``with``, in both its sync and async forms."""
    return tuple(
        node
        for node in _narrow(root, "with", ast.With, ast.AsyncWith)
        if isinstance(node, ast.With | ast.AsyncWith)
    )


def comprehensions(root: ast.AST) -> tuple[ast.ListComp | ast.SetComp | ast.GeneratorExp, ...]:
    """The comprehensions that do not build a dict."""
    return tuple(
        node
        for node in _narrow(root, "comprehension", ast.ListComp, ast.SetComp, ast.GeneratorExp)
        if isinstance(node, ast.ListComp | ast.SetComp | ast.GeneratorExp)
    )


def assignments(nodes: Iterable[ast.AST]) -> Iterator[tuple[ast.AST, ast.expr, ast.expr | None]]:
    """Every ``(node, target, value)`` written by any of `nodes`, in walk order.

    The two forms were an isinstance ladder per detector; a detector that
    forgets one branch under-reports silently."""
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield node, target, node.value
        elif isinstance(node, ast.AnnAssign):
            yield node, node.target, node.value


def attribute_calls(tree: ast.AST) -> Iterator[tuple[ast.Call, ast.Attribute]]:
    """Yield ``(call, callee)`` for every call whose callee is an attribute.

    The callee comes back narrowed alongside the call so a caller reads
    ``func.attr`` without re-asserting the type it just paid this walk to learn.
    """
    for node in nodes_of(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node, node.func


def local_nodes(node: ast.AST) -> Iterator[ast.AST]:
    """Yield ``node`` and its descendants, stopping at a nested definition.

    A ``strip`` inside a nested ``def``/``class`` must not answer a question
    about its enclosing expression; distinct from the cached ``nodes_of``."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from local_nodes(child)


@cache
def local_subtree(node: ast.AST) -> tuple[ast.AST, ...]:
    """``local_nodes(node)`` as a cached tuple -- one walk per node, walked once.

    The registry asks four questions of every node (five or six visits per
    node per pass); keyed on the node alone, which reads only its subtree."""
    return tuple(local_nodes(node))


@cache
def local_calls(node: ast.AST) -> tuple[ast.Call, ...]:
    """Every call in ``node``'s local subtree, walked once per node.

    The tree-independent half is cached -- which nodes ARE calls -- because the
    name a call resolves to is a property of the tree it is read in."""
    return tuple(child for child in local_subtree(node) if isinstance(child, ast.Call))
