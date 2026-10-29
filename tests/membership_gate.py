"""Value-membership and provenance enforcement for result primitives.

The result dataclasses' Literals are comments to the runtime until shaping
rejects a non-member with a 500; the sweeps below cover every Literal field."""

from __future__ import annotations

import ast
import pathlib
import typing
from collections.abc import Iterator, Mapping

from tests.chokepoint_walks import (
    attribute_calls,
    bare_callee,
    call_keyword_arguments,
    lineno,
    nodes_of,
    non_member_literals,
    offender,
    real_members,
)

# The result dataclasses whose Literal-typed fields are the membership contract.
RESULT_DATACLASS_NAMES = frozenset({"CrawlResult", "EngineCrawlResult", "CrawlStrategyAttempt"})

# The result primitives: the only functions that construct a wire-visible result.
RESULT_PRIMITIVES = (
    frozenset(
        {
            "failed_engine_result",
            "build_engine_result",
            "engine_result_from_exception",
            "skipped_engine_result",
            "terminal_engine_result",
            "binary_no_text_result",
            "crawl_result_from_exception",
            "crawl_result_from_engine_result",
            "terminal_crawl_result",
        }
    )
    | RESULT_DATACLASS_NAMES
)

# Fields whose declared annotation is ``X | None``.
_OPTIONAL_FIELDS = frozenset({"strategy", "error", "extraction_method", "selected_strategy"})


def literal_typed_field_members() -> FieldMembers:
    """Every Literal field across the result dataclasses, as ``field -> members``.

    The list *is* the models: members come from literal_members over
    ``app.models``, so a new field is covered the day it is added."""
    from app.models import taxonomy
    from tests.model_index import resolve_declared_type

    out: dict[str, set[str]] = {}
    for class_name in RESULT_DATACLASS_NAMES:
        cls: type = resolve_declared_type(class_name)
        fields = getattr(cls, "__dataclass_fields__", None)
        assert fields is not None, f"{class_name} is not a dataclass"
        hints = typing.get_type_hints(cls)
        for field_name in fields:
            members = taxonomy.literal_members(hints.get(field_name))
            if not members:
                continue
            out.setdefault(field_name, set()).update(members)
    return out


def _module_constants(tree: ast.AST) -> dict[str, str]:
    """Map ``SOME_CONSTANT`` -> string value for the module parsed as ``tree``.

    A bare Name that is not a module constant is a parameter or import."""
    values: dict[str, str] = {}
    for node in nodes_of(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if isinstance(node, ast.Assign):
            targets = node.targets
            value: ast.expr | None = node.value
        else:
            targets = [node.target] if node.value is not None else []
            value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                values[target.id] = value.value
    return values


#: What a keyword detector accepts as its name collection: one name, a set
#: of them, or the dict form (iterating a mapping yields the name set).
type KeywordNames = str | frozenset[str] | set[str] | Mapping[str, object] | None


def _primitive_keyword_calls(
    tree: ast.AST, *, arg_names: KeywordNames = None
) -> list[tuple[ast.Call, ast.keyword]]:
    """Yield ``(call, keyword)`` for every keyword of a result-primitive call.

    The keyword-level detectors all walk this one call shape; drifts hit every
    detector at once instead of only the one someone edited."""
    # The walker takes a name collection; ``arg_names`` is one, plus the ``dict`` form the field-set detectors
    # pass (iterating a mapping yields its keys, which is exactly the name set).
    names = None if arg_names is None else ({arg_names} if isinstance(arg_names, str) else set(arg_names))
    return list(call_keyword_arguments(tree, function_names=RESULT_PRIMITIVES, keyword_names=names))


#: The declared members of every ``Literal``-typed result field, keyed by field name.
type FieldMembers = dict[str, set[str]]


def _named_primitive_keyword_calls(
    tree: ast.AST, *, arg_names: KeywordNames = None
) -> Iterator[tuple[ast.Call, str, ast.expr]]:
    """``_primitive_keyword_calls``, each keyword paired with its name and value.

    The walk drops ``**kwargs``, so the ``arg`` assertion here is a guarantee
    the detectors can rely on instead of re-asserting per call site."""
    for call, keyword in _primitive_keyword_calls(tree, arg_names=arg_names):
        assert keyword.arg is not None, ast.unparse(keyword)
        yield call, keyword.arg, keyword.value


def generic_literal_field_offenders(tree: ast.AST, path: pathlib.Path, *, members: FieldMembers) -> list[str]:
    """Flag a literal handed to a result primitive outside its declared Literal.

    Generic over fields: members come from the models, so ``status`` is covered
    without being named; ``error`` has its own provenance story below."""
    return [
        offender(
            path,
            lineno(node),
            f"{bare_callee(node)}({field}={value!r}) is not a member of the"
            f" {field} Literal; add it to the taxonomy or pass a member",
        )
        for node, field, value in non_member_literals(
            (node, field, value, members[field])
            for node, field, value in _named_primitive_keyword_calls(tree, arg_names=members)
        )
    ]


# A name belongs to the extraction-method namespace when its stem is one of these prefixes; the target field
# is the taxonomy the primitive's parameter belongs to.
_EXTRACTION_CONSTANT_PREFIXES = ("EXTRACTION_METHOD_", "EXTRACTION_")

# Which field each namespace must never feed.
_NAMESPACE_FIELD_BANS: dict[tuple[str, ...], str] = {
    _EXTRACTION_CONSTANT_PREFIXES: "classification",
}


def _namespace_violation(name: str, field: str) -> tuple[str, ...] | None:
    """The prefix set whose constants must never feed ``field``, or None.

    An extraction constant passed as ``extraction_method=`` is intended use and
    is never reported."""
    for prefixes, banned_field in _NAMESPACE_FIELD_BANS.items():
        if field != banned_field:
            continue
        if any(name.startswith(prefix) for prefix in prefixes):
            return prefixes
    return None


def namespace_constant_offenders(tree: ast.AST, path: pathlib.Path, *, members: FieldMembers) -> list[str]:
    """Flag a constant from a foreign taxonomy passed as a Literal field.

    The value collides with a legal member, so only the name's namespace
    identifies the wrong-fields constant as wrong."""
    constants = _module_constants(tree)
    out: list[str] = []
    for node, field, value in _named_primitive_keyword_calls(tree, arg_names=members):
        if not isinstance(value, ast.Name):
            continue
        if value.id not in constants:
            continue
        hit = _namespace_violation(value.id, field)
        if hit is None:
            continue
        out.append(
            offender(
                path,
                lineno(node),
                f"{bare_callee(node)}({field}={value.id}) is a constant from the"
                f" {'/'.join(hit)} namespace, not a {field}; the value"
                f" {constants[value.id]!r} collides with a real member, which is"
                " exactly what makes this invisible to a value check -- pass the"
                " taxonomy member or the enum attribute instead",
            )
        )
    return out


# --------------------------------------------------------------------------- The runtime gate: the one place
# the class actually dies ---------------------------------------------------------------------------


class NonMemberValue(AssertionError):
    """A value crossed a result-primitive boundary outside its declared Literal.

    A named class (not bare AssertionError) so the gate firing is distinct from
    an unrelated assert in the primitive's own body."""


def assert_member(value: object, *, field: str, members: frozenset[str], where: str) -> None:
    """Verify ``value`` is a member of the ``Literal`` declared for ``field``.

    Non-``str`` is the static checker's type error, not this gate's; a
    non-member raises :class:`NonMemberValue` naming the site."""
    if not isinstance(value, str):
        raise NonMemberValue(f"{where}: {field} expected a member of its Literal, got {type(value).__name__}")
    if value not in members:
        raise NonMemberValue(
            f"{where}: {field}={value!r} is not a member of its declared Literal "
            f"(members are {sorted(members)}); add it to the taxonomy or pass a member"
        )


def assert_optional_member(value: object, *, field: str, members: frozenset[str], where: str) -> None:
    """As :func:`assert_member` but ``None`` is admitted.

    The ``| None`` fields (strategy, selected_strategy) serialise null honestly."""
    if value is None:
        return
    assert_member(value, field=field, members=members, where=where)


def field_member_sets() -> dict[str, frozenset[str]]:
    """The runtime form of :func:`literal_typed_field_members`.

    Frozen so the gate and the AST sweep cannot disagree about membership."""
    return {name: frozenset(members) for name, members in literal_typed_field_members().items()}


def assert_result_fields(
    payload: dict[str, object], *, where: str, members: dict[str, frozenset[str]]
) -> None:
    """Check every Literal-typed key of a primitive's keyword payload.

    Fields the primitive itself supplies with a default are trusted to its own
    construction; the AST sweep checks the primitive's source instead."""
    for field, value in payload.items():
        allowed = members.get(field)
        if allowed is None:
            continue
        if field in _OPTIONAL_FIELDS:
            assert_optional_member(value, field=field, members=allowed, where=where)
        else:
            assert_member(value, field=field, members=allowed, where=where)


# --------------------------------------------------------------------------- Test doubles that silently
# swallow signature drift (program 4)
_PROTOCOL_METHODS = frozenset({"run"})


def _accepts_unused_kwargs(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when ``**kwargs`` is accepted and never routed anywhere.

    A double that forwards or reads its ``**kwargs`` passes the parameter
    through; the blind shape silently implements an outdated contract."""
    kwargs = [a for a in node.args.kwonlyargs if a.arg == "kwargs"]
    if not kwargs:
        varkw = node.args.kwarg
        if varkw is None:
            return False
        kwargs = [varkw]
    kwarg = kwargs[0]
    # Routed: the double forwards it (``**kwargs`` at a call) or binds it by name (``kwargs=kwargs``,
    # ``kwargs.get(...)``) -- either way the value reaches the real contract rather than the floor.
    for sub in nodes_of(node):
        if isinstance(sub, ast.Call):
            for keyword in sub.keywords:
                if keyword.arg is None and isinstance(keyword.value, ast.Name):
                    if keyword.value.id == kwarg.arg:
                        return False
        if isinstance(sub, ast.Name) and sub.id == kwarg.arg and isinstance(sub.ctx, ast.Load):
            # Any *read* of the kwargs name inside the body means the double at least consulted it
            # (``kwargs.get(...)`` / ``kwargs[...]``).
            return False
    return True


def _is_test_double_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A ``def run(...)`` (or ``__call__``) is a double shape when it takes a
    worker/callback positional and keyword-only args -- the Protocol shape."""
    positional = [a.arg for a in node.args.posonlyargs + node.args.args if a.arg not in {"self", "cls"}]
    if node.name == "run":
        # The scheduler Protocol's two positional members.
        return len(positional) >= 2 or bool(node.args.kwonlyargs)
    if node.name == "__call__":
        # The fetcher/worker Protocol: a callable over a job/url.
        return True
    return False


def swallowing_test_double_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a Protocol-shaped test double that accepts ``**kwargs`` and drops it.

    The signature failure mode: a production callable gains a required
    parameter and every imitating double keeps passing CI, green while wrong."""
    if "tests" not in path.parts:
        return []
    out: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in _PROTOCOL_METHODS and node.name != "__call__":
            continue
        if not _is_test_double_signature(node):
            continue
        if not _accepts_unused_kwargs(node):
            continue
        out.append(
            offender(
                path,
                node.lineno,
                f"{node.name}(**kwargs) accepts and drops a kwarg a production"
                " signature may require (the failed_result refactor broke 24 tests"
                " through exactly this shape); bind the parameter by name or forward it",
            )
        )
    return out


# --------------------------------------------------------------------------- Alias-blindness: detectors must
# not key on a literal module name
_STDALIAS_MODULES = frozenset({"re"})

# decompress_body is the only entry: the cap/bypass detectors (test_bounds.py)
# key on its literal name, so an aliased call must be reported before it slips past them unseen.
_SANCTIONED_FUNC_MODULES: dict[str, frozenset[str]] = {
    "decompress_body": frozenset({"app.transport.decompression"}),
}


def _aliased_module_bindings(tree: ast.AST) -> set[str]:
    """Local names bound to an import of a module in ``_STDALIAS_MODULES``.

    Both spellings (``re`` and ``re as _re``) are captured, so the detector
    catches a dynamic pattern under either form."""
    bound: set[str] = set()
    for node in nodes_of(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _STDALIAS_MODULES:
                    bound.add(alias.asname or alias.name.split(".")[0])
    return bound


def _aliased_sanctioned_func_names(tree: ast.AST) -> dict[str, set[str]]:
    """Every local alias of a sanctioned chokepoint function, by canonical name.

    Resolved here so one map sweeps every spelling; a literal-name detector is
    blind to an alias, which is the escape hatch this closes."""
    aliases: dict[str, set[str]] = {}
    for node in nodes_of(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        for alias in node.names:
            canonical = alias.name
            sanctioned_modules = _SANCTIONED_FUNC_MODULES.get(canonical)
            if sanctioned_modules is None or module not in sanctioned_modules:
                continue
            aliases.setdefault(canonical, set()).add(alias.asname or alias.name)
    return aliases


_DYNAMIC_RE_METHODS = frozenset({"compile", "search", "match", "fullmatch", "sub", "findall", "split"})


def aliased_dynamic_pattern_re_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag ``re.<method>(<non-literal pattern>)`` under ANY import alias.

    Resolves the module binding first, so ``import re as _re`` cannot rename
    its way out of the ReDoS sweep; only the pattern argument is checked."""
    aliases = _aliased_module_bindings(tree)
    if not aliases:
        return []
    out: list[str] = []
    for node, func in attribute_calls(tree):
        if not isinstance(func.value, ast.Name):
            continue
        if func.value.id not in aliases:
            continue
        if func.attr not in _DYNAMIC_RE_METHODS:
            continue
        if not node.args:
            continue
        if isinstance(node.args[0], ast.Constant):
            continue
        out.append(
            offender(
                path,
                node.lineno,
                f"{func.value.id}.{func.attr}(<dynamic pattern>) -- alias of re;"
                " route through app.safety.safe_regex",
            )
        )
    return out


# --------------------------------------------------------------------------- Program 3 (audit): the generic
# alias-blindness detector --------------------------------------------------------------------------- Every


def aliased_sanctioned_func_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a call to a sanctioned chokepoint through an alias the spelling
    lists do not contain; unguarded chokepoints are a different finding."""
    out: list[str] = []
    aliases = _aliased_sanctioned_func_names(tree)
    if not aliases:
        return out
    # The spellings each name-matching detector hard-codes today; an alias outside the
    # set is invisible to the detector that guards the canonical name, so the sweep reports it.
    known_spellings = {"decompress_body": frozenset({"decompress_body", "_decompress_body"})}
    # _decompress_body is in the set because DECOMPRESS_CAP matches that spelling too
    # (test_bounds.py); the sweep guards the same spellings the cap detector knows.
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if call_name is None:
            continue
        for canonical, names in aliases.items():
            if call_name not in names:
                continue
            if call_name in known_spellings.get(canonical, frozenset()):
                continue
            out.append(
                offender(
                    path,
                    node.lineno,
                    f"{call_name}() is an alias of the sanctioned {canonical}; the"
                    " name-matching detectors do not know this spelling, so a missing"
                    " cap or filter would be invisible to them",
                )
            )
    return out


# --------------------------------------------------------------------------- Program 5 (hunt): the ``error=``
# field's undeclared taxonomy


def error_field_taxonomy_offenders(
    tree: ast.AST, path: pathlib.Path, *, members: frozenset[str] | None = None
) -> list[str]:
    """Flag a literal ``error=`` handed to a result primitive that is not a
    Classification member. ``members`` defaults to the live ``CrawlError``
    taxonomy; ``None`` is the honest "no error" label."""
    if members is None:
        members = real_members("CrawlError")
    return [
        offender(
            path,
            node.lineno,
            f"error={value!r} is not a Classification member, but the"
            " field is documented as carrying a stable classification code"
            " (render.py) and classify_error_code only returns members --"
            " declare it in the taxonomy or route it through the classifier",
        )
        for node, _field, value in non_member_literals(
            (node, "error", value, members)
            for node, _field, value in _named_primitive_keyword_calls(tree, arg_names={"error"})
        )
    ]
