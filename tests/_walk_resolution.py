"""Callee and import resolution: names behind calls."""

import ast
from collections.abc import Callable, Iterable, Iterator
from functools import lru_cache

from tests._walk_primitives import nodes_of
from tests._walk_readers import assignment_targets, call_nodes, local_calls


def _call_leaf_name(call: ast.Call) -> str:
    """The bare name a call is made through: ``uuid4()`` -> ``uuid4``, ``uuid.uuid4()`` -> ``uuid4``.

    ``getattr(call.func, "id", None) or getattr(call.func, "attr", None)`` is how a
    detector asks "is this a call to uuid4/HTTPException" without caring whether the
    module imported the name or reached it through a module. Spelled out it is one
    line, and it was written out separately by every detector that asked, which is
    how one of them ended up comparing against ``None``.
    """
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None) or ""


def bare_callee(node: ast.AST) -> str | None:
    """The bare function name ``node`` calls, or None when it is not such a call.

    ``int(x)``, ``uuid4()`` and ``_persist(...)`` are spelled
    ``ast.Call(func=ast.Name)``. A detector that wants only those has to test the
    shape itself, and detectors kept testing it themselves: the ``isinstance``
    pair this replaces was written out separately by each one, which is how the
    jscpd gate came to flag the same five lines in two files. Unlike
    :func:`_call_leaf_name` this does not fall back to an attribute callee, so
    ``obj.int(x)`` is not a match.
    """
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def calls_named_leaf(tree: ast.AST, names: frozenset[str] | set[str]) -> Iterator[ast.Call]:
    """Yield calls whose leaf callee name is one of ``names``.

    :func:`calls_to_any` keys on the callee *expression* (a bare ``Name``), which
    is the right question for a helper a module imported directly and the wrong
    one for a chokepoint reached as ``module.name()``. Detectors for the second
    shape each re-derived the leaf name, so this is that derivation, once.
    """
    for call in call_nodes(tree):
        if _call_leaf_name(call) in names:
            yield call


def string_argument(call: ast.Call, index: int = 0) -> str | None:
    """The ``index``-th positional argument of ``call`` if it is a string literal, else ``None``.

    Half the detectors in this tree are "find the call and look at what it was
    handed" -- ``x.get("selector")``, ``conn.execute("PRAGMA journal_mode=...")``,
    ``foo("some_name")``. Each wrote out the same four lines to do it:

        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)

    Which is three lines of clone per detector and, worse, four chances to spell
    the narrowing slightly differently. Detectors here are also expected to prove
    they bite, and a four-line guard in the middle of one is a thing to retype
    rather than reason about.

    ``None`` for a non-literal rather than a truthiness check on the value,
    because ``""`` is a legitimate string argument and a caller asking "is this
    the empty string?" should not have to defeat this helper to ask it.
    """
    if len(call.args) <= index:
        return None
    argument = call.args[index]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    return None


def single_target_name_assignments(
    nodes: Iterable[ast.AST],
    value_predicate: Callable[[ast.expr], bool],
) -> Iterator[tuple[ast.Assign, str]]:
    """Yield ``(node, name)`` for single-target Name assignments whose value matches.

    ``a, b = ...`` and ``obj.x = ...`` are deliberately not yielded: both callers
    are asking "which plain local names hold this kind of value", and a
    multi-target or attribute target is a different question.
    """
    for node in nodes:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and value_predicate(node.value):
            yield node, target.id


def local_call_leaf_names(tree: ast.AST, node: ast.AST) -> Iterator[tuple[str, ast.Call]]:
    """Yield ``(lowercased_leaf_name, call)`` for every call inside ``node``.

    ``resolved_call_name(tree, call).rsplit(".", 1)[-1].lower()`` is how a detector
    asks "is this a call to strip/lower/format/parse" without caring which module
    imported it. Spelled out it is four lines, and it was written out separately
    by every detector that asked.
    """
    for call in local_calls(node):
        yield resolved_call_name(tree, call).rsplit(".", 1)[-1].lower(), call


def _getattr_literal_name(call: ast.Call) -> str | None:
    """The attribute name of ``getattr(obj, "name")`` when it is a literal string.

    ``getattr(x, "strip")`` and ``x.strip()`` name the same method, and a detector
    that recognizes only the second is blind to the first. Returned lowercased so
    the caller compares against a lowercase name set.
    """
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        value = call.args[1].value
        if isinstance(value, str):
            return value.lower()
    return None


def _calls_naming_method(
    tree: ast.AST,
    node: ast.AST,
    method_names: frozenset[str],
) -> Iterator[ast.Call]:
    """Yield calls inside ``node`` that reach one of ``method_names``.

    Reaches a method two ways on purpose: a direct call (``s.strip()``) and a
    literal-named ``getattr`` (``getattr(s, "strip")()``). Aliasing a method
    through a variable is out of reach for any AST-only check and is not claimed
    to be.
    """
    for name, call in local_call_leaf_names(tree, node):
        if name in method_names:
            yield call
        elif name == "getattr" and _getattr_literal_name(call) in method_names:
            yield call


def has_method_call(tree: ast.AST, node: ast.AST, method_names: frozenset[str]) -> bool:
    """Does ``node`` reach any of ``method_names``? The boolean form."""
    return next(_calls_naming_method(tree, node, method_names), None) is not None


def calls_to_any(tree: ast.AST, function_names) -> Iterator[ast.Call]:
    """Yield direct calls whose callee is any of ``function_names``.

    The set-valued sibling of :func:`calls_named`. Several detectors track a set
    of names they *discovered* -- aliases, a builder propagated through
    assignments -- rather than one literal, and restating the
    ``isinstance(node.func, ast.Name) and node.func.id in names`` filter inline
    is how two detectors end up carrying the same scan shape twice.
    """
    for node in nodes_of(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in function_names:
            yield node


def named_calls_to_any(tree: ast.AST, function_names) -> Iterator[tuple[ast.Call, str]]:
    """:func:`calls_to_any`, paired with the callee's name.

    ``calls_to_any`` already filters on ``isinstance(node.func, ast.Name)``, so
    the name is always right there -- but ``Call.func`` is declared as the whole
    expression type, so a detector that wanted to *report* the callee had to
    re-assert that same narrowing, and three of them in the result-shape sweep
    were each carrying a copy. The assertion lives here, once, and a detector
    that reports a name it did not actually match is the failure this removes.
    """
    for call in calls_to_any(tree, function_names):
        assert isinstance(call.func, ast.Name), ast.unparse(call.func)
        yield call, call.func.id


def calls_named(tree: ast.AST, function_name: str) -> Iterator[ast.Call]:
    """Yield direct calls to one named function in an AST tree."""
    return calls_to_any(tree, {function_name})


def call_keyword_arguments(
    tree: ast.AST,
    *,
    function_names: str | frozenset[str] | set[str],
    keyword_names: str | tuple[str, ...] | set[str] | None = None,
) -> Iterator[tuple[ast.Call, ast.keyword]]:
    """Yield ``(call, keyword)`` for selected keywords of direct calls to named functions.

    The one walk behind "what was spelled at these call sites". ``keyword_names``
    of ``None`` selects every named keyword (``**kwargs`` carries ``arg is None``
    and is never selectable -- it has no name to be a member of). Callers that
    only want the value use :func:`call_keyword_values`; callers that need the
    keyword's own name -- a Literal field's member set is keyed by it -- come
    here, because re-walking to recover the name is how two detectors end up
    matching a value against the wrong axis.
    """
    names = {function_names} if isinstance(function_names, str) else function_names
    keywords = (
        None
        if keyword_names is None
        else ({keyword_names} if isinstance(keyword_names, str) else set(keyword_names))
    )
    for node in calls_to_any(tree, names):
        seen_keywords: set[str | None] = set()
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in seen_keywords:
                continue
            if keywords is not None and keyword.arg not in keywords:
                continue
            seen_keywords.add(keyword.arg)
            yield node, keyword


def call_keyword_values(
    tree: ast.AST,
    *,
    function_names: str | frozenset[str],
    keyword_names: str | tuple[str, ...],
) -> Iterator[tuple[ast.Call, ast.expr]]:
    """Yield selected keyword values from direct calls to named functions."""
    for node, keyword in call_keyword_arguments(
        tree,
        function_names=function_names,
        keyword_names=keyword_names,
    ):
        yield node, keyword.value


def _static_string(expr: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        left = _static_string(expr.left, bindings)
        right = _static_string(expr.right, bindings)
        return left + right if left is not None and right is not None else None
    if isinstance(expr, ast.JoinedStr):
        parts = [_static_string(value, bindings) for value in expr.values]
        if any(part is None for part in parts):
            return None
        return "".join(part for part in parts if part is not None)
    return None


@lru_cache(maxsize=256)
def _import_bindings(tree: ast.AST) -> dict[str, str]:
    bindings: dict[str, str] = {}
    nodes = nodes_of(tree)
    for node in nodes:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                bindings[local] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                bindings[local] = alias.name if alias.asname else local
    assignments = [node for node in nodes if isinstance(node, (ast.Assign, ast.AnnAssign))]
    changed = True
    while changed:
        changed = False
        for node in assignments:
            value = node.value
            if value is None:
                continue
            # Two candidate spellings, and the second is the fallback for an empty first -- including for a
            # call, which resolves to nothing.
            named = "" if isinstance(value, ast.Call) else _resolved_expr(value, bindings)
            canonical = named or _static_string(value, bindings)
            if not canonical:
                continue
            targets = assignment_targets(node)
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in bindings:
                    bindings[target.id] = canonical
                    changed = True
    return bindings


def _resolved_expr(expr: ast.AST, bindings: dict[str, str]) -> str:
    """Resolve import aliases and constant-folded ``getattr`` names.

    Only literals, concatenations, and constant f-strings are folded for an
    attribute name. Runtime-computed values remain opaque by design.
    """
    if isinstance(expr, ast.Name):
        return bindings.get(expr.id, expr.id)
    if isinstance(expr, ast.Attribute):
        base = _resolved_expr(expr.value, bindings)
        return f"{base}.{expr.attr}" if base else expr.attr
    if isinstance(expr, ast.Call):
        callee = _resolved_expr(expr.func, bindings)
        if callee.rsplit(".", 1)[-1] == "getattr" and len(expr.args) >= 2:
            attribute = _static_string(expr.args[1], bindings)
            base = _resolved_expr(expr.args[0], bindings)
            if attribute is not None and base:
                return f"{base}.{attribute}"
        return callee
    return ""


def resolved_expr_name(tree: ast.AST, expr: ast.AST) -> str:
    """Return an expression's import-resolved dotted name for AST detectors."""
    return _resolved_expr(expr, _import_bindings(tree))


def resolved_call_name(tree: ast.AST, call: ast.Call) -> str:
    """Return a call's import-resolved dotted name for AST detectors."""
    return resolved_expr_name(tree, call)
