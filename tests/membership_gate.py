"""Generic value-membership and provenance enforcement for result primitives.

Programs 1 and 2 of the chokepoint review, in one module because they are the
same class seen twice.

The class
---------
``app/orchestration/engine_result.py`` and ``render_result.py`` are the chokepoints for
building a result; the dataclasses they fill carry ``Literal`` annotations. The annotations are
*comments to the runtime*: ``CrawlResult`` is a plain frozen dataclass, so
nothing rejects ``classification="url_budget_exceeded"`` until the value
reaches the strict pydantic enum at response shaping, where a single non-member
rejects the whole batch with a 500. Two escapes shipped that way and were each
closed by a *per-field* detector written after the fact -- one for
``classification``, one for ``strategy``. A per-field detector is a list a
nobody extends, so the class re-emerged at every field not on the list.

This module closes the class on the field axis, in two halves.

1. Generic literal membership (:func:`generic_literal_field_offenders`).
   Enumerates ``(dataclass, field, declared Literal)`` from ``app/models`` at
   test time and checks every literal handed to a result primitive against the
   union of that field's members. Nothing here names ``classification`` or
   ``strategy``; a new Literal field in a result dataclass is covered the day
   it is added.

2. Named-constant provenance (:func:`namespace_constant_offenders`).
   A literal detector is blind to ``classification=SOME_CONSTANT`` -- the
   historical escape was exactly that shape: ``EXTRACTION_METHOD_BINARY_NO_TEXT``
   is a real module constant whose *value* ("binary_no_text") is a legal
   classification member, so even value resolution admits it. The defect is
   that a constant from the *extraction namespace* was used as a
   *classification*. This checks the name's namespace, which is the property
   that was actually wrong; value resolution alone cannot see it, because the
   collision is precisely what makes the bug invisible to a value check.

Both halves sweep the whole ``app/`` tree via ``app_trees()``.
"""

from __future__ import annotations

import ast
import pathlib
import typing
from collections.abc import Iterator
from typing import Any

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

# The result dataclasses whose Literal-typed fields are the membership
# contract. Direct construction of one anywhere in app/ is checked by the
# sweep below as well -- the primitives are the sanctioned site, but the
# AST sweep does not assume that is the only site, so a direct build cannot
# escape membership checking just by not going through a primitive.
RESULT_DATACLASS_NAMES = frozenset({"CrawlResult", "EngineCrawlResult", "CrawlStrategyAttempt"})

# The result primitives: the only functions that construct a wire-visible
# result. Not a file list -- a name set, because the AST matches on name and a
# primitive in a new module is a name nobody has to register here for the
# field-derived sweep to cover it. Unioned with the dataclasses themselves so
# a direct build is checked by the same sweep as a primitive call.
RESULT_PRIMITIVES = (
    frozenset(
        {
            "_failed_engine_result",
            "build_engine_result",
            "engine_result_from_exception",
            "_skipped_engine_result",
            "_terminal_engine_result",
            "binary_no_text_result",
            "crawl_result_from_exception",
            "crawl_result_from_engine_result",
            "terminal_crawl_result",
        }
    )
    | RESULT_DATACLASS_NAMES
)

# Fields whose declared annotation is ``X | None``. ``None`` is the honest "no
# value" label for these (strategy: "no strategy was attempted"; error/
# extraction_method: "none produced"), and the wire model serializes it as
# null -- it is not an invented member and must not be reported.
_OPTIONAL_FIELDS = frozenset({"strategy", "error", "extraction_method", "selected_strategy"})


def literal_typed_field_members() -> FieldMembers:
    """Every Literal field across the result dataclasses, as ``field -> members``.

    Enumerated from ``app.models`` at test time: the union over the dataclasses
    is used so a field name shared by two classes (``status`` and
    ``classification`` are on all three) is checked against one set rather than
    three. Nothing in this module or its tests transcribes a field list -- the
    list *is* the models, which is what keeps a new field covered instead of
    remembered.

    Membership comes from ``app.models.taxonomy.literal_members`` rather than
    ``typing.get_args``, and the filter is "has members" rather than "origin is
    ``Literal``", because a taxonomy alias is allowed to be composed. ``error``
    is ``Classification | PIPELINE_ONLY_CRAWL_ERRORS`` and ``strategy`` is
    ``CrawlStrategyName | None``; under the old filter both have a Union origin,
    so both were skipped -- not reported, just absent, which is the shape of
    defect this module exists to prevent. ``literal_members`` is total over
    compositions, so a field is covered by virtue of declaring members at all.
    """
    from app.models import taxonomy
    from tests.model_index import resolve_declared_type

    out: dict[str, set[str]] = {}
    for class_name in RESULT_DATACLASS_NAMES:
        cls = resolve_declared_type(class_name)
        hints = typing.get_type_hints(cls)
        for field_name in cls.__dataclass_fields__:
            members = taxonomy.literal_members(hints.get(field_name))
            if not members:
                continue
            out.setdefault(field_name, set()).update(members)
    return out


def _module_constants(tree: ast.AST) -> dict[str, str]:
    """Map ``SOME_CONSTANT`` -> string value for the module parsed as ``tree``.

    Used by the provenance check to recognize a name that resolves at all: a
    bare ``Name`` that is not a module constant is a parameter or an import and
    is what callers should write.
    """
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


def _primitive_keyword_calls(
    tree: ast.AST, *, arg_names: str | frozenset[str] | set[str] | dict[str, Any] | None = None
) -> list[tuple[ast.Call, ast.keyword]]:
    """Yield ``(call, keyword)`` for every keyword of a result-primitive call.

    The keyword-level detectors all walk the same shape -- a call whose
    ``func`` is a bare name in ``RESULT_PRIMITIVES`` -- and disagree only
    about what the keyword VALUE means (literal membership, constant
    namespace, error-code provenance). This is the one walk; if the set of
    calls that count as primitive calls drifts, it drifts for every
    detector at once instead of only the one someone edited.

    The walk itself is :func:`call_keyword_arguments` -- the same one the
    non-primitive keyword detectors use -- with this module's registry as the
    name set. Only the "which calls" question is local; "which keywords of
    them" was a second implementation of a rule that already had an owner, and
    two implementations of a walk is how the two halves of a keyword detector
    end up disagreeing about ``**kwargs``.
    """
    # The walker takes a name collection; ``arg_names`` is one, plus the
    # ``dict`` form the field-set detectors pass (iterating a mapping yields its
    # keys, which is exactly the name set). Normalized here so the walker's own
    # declared union -- which does not include ``dict`` -- is not bypassed.
    names = None if arg_names is None else ({arg_names} if isinstance(arg_names, str) else set(arg_names))
    return list(call_keyword_arguments(tree, function_names=RESULT_PRIMITIVES, keyword_names=names))


#: The declared members of every ``Literal``-typed result field, keyed by field
#: name. The map is derived from ``app/models`` rather than written down, so a
#: Literal field nobody has listed is a gap in the model rather than a silent
#: hole in the sweep -- which is why both field detectors name this type instead
#: of repeating ``dict[str, set[str]]`` in their signatures.
type FieldMembers = dict[str, set[str]]


def _named_primitive_keyword_calls(
    tree: ast.AST, *, arg_names: str | frozenset[str] | set[str] | dict[str, Any] | None = None
) -> Iterator[tuple[ast.Call, str, ast.expr]]:
    """``_primitive_keyword_calls``, each keyword paired with its name and value.

    The underlying walk already drops ``**kwargs`` -- a keyword with no name is
    not a member of any field's set -- but it hands back an ``ast.keyword``,
    whose ``arg`` is declared ``str | None``, so every detector that keyed a
    field's member set by that name had to re-assert a fact the walk already
    guarantees. Three of the four were doing exactly that.
    """
    for call, keyword in _primitive_keyword_calls(tree, arg_names=arg_names):
        assert keyword.arg is not None, ast.unparse(keyword)
        yield call, keyword.arg, keyword.value


def generic_literal_field_offenders(tree: ast.AST, path: pathlib.Path, *, members: FieldMembers) -> list[str]:
    """Flag a literal handed to a result primitive outside its declared Literal.

    Generic over fields: ``members`` comes from
    :func:`literal_typed_field_members`, so the sweep covers ``status`` and
    ``classification`` (and any future Literal field) without naming either.
    A non-Literal field (``error``, ``extraction_method``) is not in the map and
    is not checked here -- ``error`` has its own provenance story below. Which
    values count as spelled non-members is
    :func:`chokepoint_walks.non_member_literals`, shared with the ``error``
    sweep and with the detectors in ``tests/invariants/``.
    """
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


# ---------------------------------------------------------------------------
# Named-constant provenance (program 2)
# ---------------------------------------------------------------------------

# The wrong-namespace escape: a constant whose *name* belongs to one taxonomy
# was consumed as a member of another. Value membership cannot see this,
# because the historical collision was a constant whose value is ALSO a legal
# member of the target field (EXTRACTION_METHOD_BINARY_NO_TEXT == "binary_no_text").
# The namespace of the NAME is the property that was wrong, so that is what a
# correct detector keys on.
#
# A name belongs to the extraction-method namespace when its stem is one of
# these prefixes; the target field is the taxonomy the primitive's parameter
# belongs to.
_EXTRACTION_CONSTANT_PREFIXES = ("EXTRACTION_METHOD_", "EXTRACTION_")

# Which field each namespace must never feed. Extraction-method constants are
# provenance labels for how bytes were parsed; the classification field is a
# page outcome. The two axes are orthogonal by design (see
# app/vocabulary/extraction.py: "These are output-pipeline options, strictly orthogonal to
# fetch strategies"), and the historical defect was exactly their collision.
_NAMESPACE_FIELD_BANS: dict[tuple[str, ...], str] = {
    _EXTRACTION_CONSTANT_PREFIXES: "classification",
}


def _namespace_violation(name: str, field: str) -> tuple[str, ...] | None:
    """The prefix set whose constants must never feed ``field``, or None.

    A name is only reported when its prefix is *banned from the field it is
    passed to*: an extraction constant passed as ``extraction_method=`` is the
    intended use and never reported.
    """
    for prefixes, banned_field in _NAMESPACE_FIELD_BANS.items():
        if field != banned_field:
            continue
        if any(name.startswith(prefix) for prefix in prefixes):
            return prefixes
    return None


def namespace_constant_offenders(tree: ast.AST, path: pathlib.Path, *, members: FieldMembers) -> list[str]:
    """Flag a constant from a foreign taxonomy passed as a Literal field.

    Catches ``_failed_engine_result(classification=EXTRACTION_METHOD_BINARY_NO_TEXT)``:
    a Name whose resolution belongs to the extraction-method namespace being
    consumed as a classification. The value happens to be a legal
    classification member, so a value-membership check admits it -- the name's
    namespace is the only signal that the constant was reached for the wrong
    reason. Names that are not module constants (parameters, imports) are what
    a correct call site writes and pass without comment.
    """
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


# ---------------------------------------------------------------------------
# The runtime gate: the one place the class actually dies
# ---------------------------------------------------------------------------
#
# Every AST detector above is a property of *source text*: it can see a literal
# or a name, and nothing more. The historical escape is exactly the shape that
# defeats that: a local variable is assigned a wrong-namespace constant
# (``classification = EXTRACTION_METHOD_BINARY_NO_TEXT``), then passed to the
# primitive as ``classification=classification``. The kwarg is a bare Name, it
# is not a module constant, and it is not a literal -- so the literal detector
# sees nothing, the provenance detector sees nothing, and the value sails into
# the strict enum at response shaping, which rejects the whole batch with a 500.
#
# The general property cannot be enforced by reading source text, because the
# wrong value can arrive through any number of indirections. It CAN be enforced
# at the boundary: the primitives are the chokepoint by construction (the AST
# sweep above proves every site routes through them), so a membership check
# inside the primitive sees every value regardless of how it got there.
#
# The gate is deliberately narrow: it checks membership of the declared Literal
# for the fields that carry one, on the values that actually reach the
# primitive. It raises ``NonMemberValue`` rather than coercing, because a
# wrong value is a bug to fix at the producer, not a value to silently repair
# (silently repairing it is how a taxonomy drift hides for a release).


class NonMemberValue(AssertionError):
    """A value crossed a result-primitive boundary outside its declared Literal.

    A distinct exception class (not bare ``AssertionError``) so a failing test
    reports the *chokepoint* firing instead of a generic assertion, and so an
    unrelated ``assert`` in a primitive's body can never be mistaken for the
    membership contract.
    """


def assert_member(value: object, *, field: str, members: frozenset[str], where: str) -> None:
    """Verify ``value`` is a member of the ``Literal`` declared for ``field``.

    ``members`` is the field's declared value set. Non-``str`` values are a
    type error the static checker owns, not this gate's contract; a non-member
    string raises :class:`NonMemberValue` naming the site, which is the message
    that points at the producer rather than the primitive.
    """
    if not isinstance(value, str):
        raise NonMemberValue(f"{where}: {field} expected a member of its Literal, got {type(value).__name__}")
    if value not in members:
        raise NonMemberValue(
            f"{where}: {field}={value!r} is not a member of its declared Literal "
            f"(members are {sorted(members)}); add it to the taxonomy or pass a member"
        )


def assert_optional_member(value: object, *, field: str, members: frozenset[str], where: str) -> None:
    """As :func:`assert_member` but ``None`` is admitted.

    ``CrawlStrategyAttempt.strategy`` and ``EngineCrawlResult.selected_strategy``
    are ``CrawlStrategyName | None``: "no strategy was attempted" is an honest
    label rather than an invented member, and the wire model serializes null.
    """
    if value is None:
        return
    assert_member(value, field=field, members=members, where=where)


def field_member_sets() -> dict[str, frozenset[str]]:
    """The runtime form of :func:`literal_typed_field_members`.

    Frozen so a caller cannot mutate the set the gate checks against; the gate
    reads the same models the AST sweep does, so the two halves cannot disagree
    about what is a member.
    """
    return {name: frozenset(members) for name, members in literal_typed_field_members().items()}


def assert_result_fields(
    payload: dict[str, object], *, where: str, members: dict[str, frozenset[str]]
) -> None:
    """Check every Literal-typed key of a primitive's keyword payload.

    ``payload`` is the kwargs the primitive was called with. Only fields the
    models declare as Literals are checked, and only when present in the
    payload -- a field with a default the primitive itself supplies is trusted
    to the primitive's own construction (``status="failed"`` in
    ``_failed_engine_result``), which is checked by the AST sweep over the
    primitive's own source rather than re-asserted per call.
    """
    for field, value in payload.items():
        allowed = members.get(field)
        if allowed is None:
            continue
        if field in _OPTIONAL_FIELDS:
            assert_optional_member(value, field=field, members=allowed, where=where)
        else:
            assert_member(value, field=field, members=allowed, where=where)


# ---------------------------------------------------------------------------
# Test doubles that silently swallow signature drift (program 4)
# ---------------------------------------------------------------------------

# Production callables a test double is structurally imitating. A double that
# accepts ``**kwargs`` and never routes them is invisible to a signature
# change: the refactor that added the required ``failed_result`` kwarg to
# ``DomainScheduler.run`` broke 24 tests silently for exactly this reason.
#
# Kept as (kind, call-name, params) triples rather than a file list: the sweep
# matches a double by shape (it defines the method/callable a Protocol
# requires), and a new Protocol is a new triple to add here.
_PROTOCOL_METHODS = frozenset({"run"})


def _accepts_unused_kwargs(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when ``**kwargs`` is accepted and never routed anywhere.

    A double that forwards ``**kwargs`` to the real callable (``real.run(...,
    **kwargs)``) is NOT blind -- it passes the parameter through, so a signature
    change surfaces at the real call. The blind shape is the one that accepts
    and drops: the double silently implements an outdated contract and CI stays
    green while the production caller's new required kwarg vanishes.
    """
    kwargs = [a for a in node.args.kwonlyargs if a.arg == "kwargs"]
    if not kwargs:
        varkw = node.args.kwarg
        if varkw is None:
            return False
        kwargs = [varkw]
    kwarg = kwargs[0]
    # Routed: the double forwards it (``**kwargs`` at a call) or binds it by
    # name (``kwargs=kwargs``, ``kwargs.get(...)``) -- either way the value
    # reaches the real contract rather than the floor.
    for sub in nodes_of(node):
        if isinstance(sub, ast.Call):
            for keyword in sub.keywords:
                if keyword.arg is None and isinstance(keyword.value, ast.Name):
                    if keyword.value.id == kwarg.arg:
                        return False
        if isinstance(sub, ast.Name) and sub.id == kwarg.arg and isinstance(sub.ctx, ast.Load):
            # Any *read* of the kwargs name inside the body means the double at
            # least consulted it (``kwargs.get(...)`` / ``kwargs[...]``).
            return False
    return True


def _is_test_double_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A ``def run(...)`` (or ``__call__``) is a double shape when it takes a
    worker/callback positional and keyword-only args -- the Protocol shape.

    ``DomainScheduler.run``'s signature is
    ``(jobs, worker, on_result=None, *, ..., failed_result=...)``. A double of
    it carries ``jobs``/``worker`` positionals and keyword-only params; a
    same-named method on an unrelated class does not, so it stays unflagged.
    """
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

    The class of failure this forecloses: a production callable's signature
    gains a required parameter, and every double that imitated it keeps
    passing CI because ``**kwargs`` accepted the new argument and discarded it.
    The test then exercises a double of a contract that no longer exists, which
    is the project's signature failure mode -- green while wrong.
    """
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


# ---------------------------------------------------------------------------
# Alias-blindness: detectors must not key on a literal module name
# ---------------------------------------------------------------------------

# ``import re as _re`` followed by ``_re.compile(<pattern>)`` is invisible to a
# detector that matches on the name ``re``. The historical detectors did match
# on that name, so an alias was an escape hatch -- not a malicious one, but a
# real one: app/orchestration/fetch_helpers.py carries that shape today (with safe
# literal patterns), and a *dynamic* pattern behind the same alias would sail
# past every guard. Keying on the import binding instead of the literal name
# closes the class: whichever alias a module picks, the binding is resolved
# first and the pattern check applies to it.
#
# The same class affects the sanctioned *chokepoint* functions
# (decompress_body, admit_or_none, ...): ``from x import decompress_body
# as dec`` then ``dec(...)`` is invisible to a detector that matches on the
# literal import name. Resolving the import binding closes both at once, which
# is why this is one mechanism rather than two.
_STDALIAS_MODULES = frozenset({"re"})

# Chokepoint functions whose call sites are guarded by name-matching detectors
# elsewhere in this suite. A detector keyed on a literal name cannot see an
# alias of that name, so the set of aliases is resolved from imports instead.
_SANCTIONED_FUNC_MODULES: dict[str, frozenset[str]] = {
    "decompress_body": frozenset({"app.transport.decompression"}),
}


def _aliased_module_bindings(tree: ast.AST) -> set[str]:
    """Local names bound to an import of a module in ``_STDALIAS_MODULES``.

    ``import re`` binds ``re``; ``import re as _re`` binds ``_re``. Returning
    both means the detector below catches a dynamic pattern under either form,
    and the old literal-name check is subsumed rather than duplicated.
    """
    bound: set[str] = set()
    for node in nodes_of(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _STDALIAS_MODULES:
                    bound.add(alias.asname or alias.name.split(".")[0])
    return bound


def _aliased_sanctioned_func_names(tree: ast.AST) -> dict[str, set[str]]:
    """Every local alias of a sanctioned chokepoint function, by canonical name.

    ``from app.transport.decompression import decompress_body`` binds
    ``decompress_body``; ``... as dec`` binds ``dec``. The detectors that guard
    chokepoint call sites match on the literal import name, so an alias is an
    escape hatch in the same way ``import re as _re`` is. This resolves the
    binding so a detector can sweep every alias of every sanctioned function
    from one map instead of a per-detector list of spellings.
    """
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

    This subsumes the two existing name-anchored detectors
    (``_dynamic_pattern_re_calls`` and ``_no_direct_regex_engine_import``'s
    call shape): it resolves the module binding first, so ``import re as _re``
    cannot rename its way out of the sweep. Only the *pattern* argument is
    checked -- ``re.search(LITERAL, text)`` is safe, a dynamic PATTERN is the
    ReDoS hazard.
    """
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


# ---------------------------------------------------------------------------
# Program 3 (audit): the generic alias-blindness detector
# ---------------------------------------------------------------------------
#
# Every name-matching detector in this suite is blind to ``from x import y as
# z``: it matches on the literal import name, and the alias is a different
# spelling. The existing detectors were each patched to add the one alias then
# present in the tree (``_decompress_body`` was added to ``_DECOMPRESS_FUNCS``),
# which is exactly the per-representative habit this audit was written to find:
# the list is extended after each new alias appears, so a future alias is
# invisible until someone notices.
#
# The structural fix is to stop matching on spellings: resolve the import
# binding and sweep the binding. ``_aliased_module_bindings`` does it for
# ``re``; the detector below does it for every sanctioned chokepoint function
# the suite guards, so a new alias is covered the day it is written rather than
# the day someone extends a frozenset.


def aliased_sanctioned_func_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag a call to a sanctioned chokepoint through an alias the spelling
    lists do not contain.

    This is the audit of the name-matching detectors, not a duplicate: it
    reports any alias the hand-kept lists missed, which is how the
    one-representative habit is caught rather than patched. Only calls are
    checked, and only for functions whose call-site shape is guarded elsewhere
    -- an unguarded chokepoint is a different finding.
    """
    out: list[str] = []
    aliases = _aliased_sanctioned_func_names(tree)
    if not aliases:
        return out
    # The spellings each name-matching detector hard-codes today. An alias
    # outside this set is invisible to the detector that guards the canonical
    # name, which is the property this sweep reports.
    known_spellings = {"decompress_body": frozenset({"decompress_body", "_decompress_body"})}
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


# ---------------------------------------------------------------------------
# Program 5 (hunt): the ``error=`` field's undeclared taxonomy
# ---------------------------------------------------------------------------
#
# ``CrawlResult.error`` / ``EngineCrawlResult.error`` / ``CrawlStrategyAttempt.error``
# are declared ``str | None`` -- not a Literal. Every other axis on these
# dataclasses that crosses a boundary is a Literal or an enum; ``error`` is the
# one field where the annotation is the *only* guard, which is the exact shape
# this project's history repeats.
#
# The field's own documentation says the value must be a "stable classification"
# (app/api/render.py: the wire-visible error must be a stable classification,
# never raw exception text). ``classify_error_code`` honors that -- every value
# it returns is a ``Classification`` member. The hand-written literals are the
# escape: ``"worker_failed"`` (render.py), ``"unsupported_strategy"`` and
# ``"browser_worker_unavailable"`` (engine.py) are emitted into ``error=`` and
# are NOT Classification members, so the comment's contract holds for the
# classified path and silently fails for the hand-written one.
#
# This sweep reports a literal ``error=`` that is not a Classification member.
# It is deliberately not a hard failure for the same reason the field is str:
# no taxonomy has been declared for it, so a value being outside
# Classification is not yet a *defined* defect -- it is a missing chokepoint,
# which is the finding to report, not the instance to patch.


def error_field_taxonomy_offenders(
    tree: ast.AST, path: pathlib.Path, *, members: frozenset[str] | None = None
) -> list[str]:
    """Flag a literal ``error=`` handed to a result primitive that is not a
    Classification member.

    ``members`` defaults to the live ``CrawlError`` members -- the *union* of
    the classification and error-code taxonomies, which is what makes
    ``CrawlError`` a taxonomy rather than a restatement of one of them. The
    default is resolved, not transcribed. A
    non-literal value is what a correct caller writes and passes without
    comment; ``None`` is the honest "no error" label and is admitted -- that
    admission is :func:`chokepoint_walks.spelled_non_member`, not a branch
    here.
    """
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
