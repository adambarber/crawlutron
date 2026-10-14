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

    A detector's rule is very often "an expression that coerces a possibly-absent
    value into a present one", and in Python that is spelled ``x or y``. The
    shape appears once per rule, so it is a vocabulary word rather than a guard
    each rule re-derives: written out, the three-line guard below was the same
    clone in every file that needed it, which is exactly what the clone gate
    exists to prevent.
    """
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

    ``ast.Assign`` carries a list and ``ast.AnnAssign`` a single node, so every
    detector that cares what a statement binds had to branch on the form before
    it could look at a target. That branch is four lines, it reads as incidental,
    and it was written out in six detectors across four files -- which is how one
    of them grew a variant that handled only ``Assign`` and quietly read a tuple
    where an annotated assignment was written. The question every caller is
    actually asking is "what does this statement bind", and this is that answer.
    """
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

    `assignments` covers the three statement forms and yields the target/value
    triple; this is the pair that leaves out ``ast.AnnAssign``, because a rule
    about rebinding an existing name wants the writes carrying no annotation.
    """
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

    "What does this node assign, and to what?" has exactly two answers --
    ``ast.Assign``'s ``targets``/``value`` and ``ast.AnnAssign``'s single
    ``target``/``value`` -- and detectors kept writing both as an ``isinstance``
    ladder over a local ``targets`` list they had to declare and re-initialise
    on every node. The ladder is the failure mode: a detector that writes the
    ``AnnAssign`` branch and forgets the ``Assign`` one under-reports silently,
    which is indistinguishable from a clean tree.

    The owning node rides along because a detector almost always reports against
    it, and recovering it afterwards means re-walking to line the two sequences
    up -- which is where a mismatched pair goes unnoticed.
    """
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

    A detector asking "does THIS expression call strip()" must not be answered by
    a ``strip`` in a function defined inside it, so the walk does not descend
    into a nested ``def``/``async def``/``class`` body. Distinct from
    :func:`nodes_of`, which is whole-subtree and cached.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield from local_nodes(child)


@cache
def local_subtree(node: ast.AST) -> tuple[ast.AST, ...]:
    """``local_nodes(node)`` as a cached tuple -- one walk per node, walked once.

    ``local_nodes`` is a generator, so a detector asking a question about a
    node's local subtree walked it again from the top. The property registry
    asks four questions per node, and asks them of every node of every module,
    so one node was traversed five or six times per pass: sixty-odd million
    node visits for a single test run. Caching the walk makes each of those a
    tuple read.

    Keyed on the node alone, because the walk reads only that node's own
    subtree and AST nodes are compared by identity. Same reasoning, and the
    same memory profile, as the cached ``nodes_of`` above.
    """
    return tuple(local_nodes(node))


@cache
def local_calls(node: ast.AST) -> tuple[ast.Call, ...]:
    """Every call in ``node``'s local subtree, walked once per node.

    ``local_call_leaf_names`` re-walked the subtree on every call, and it is in
    turn the body of ``_calls_naming_method`` and ``has_method_call``, so one
    question about a node cost a fresh walk of it. Only the tree-independent
    half is cached -- which nodes are calls -- because the name a call resolves
    to is a property of the tree it is read in, and a node reached through two
    modules is read in each.
    """
    return tuple(child for child in local_subtree(node) if isinstance(child, ast.Call))
