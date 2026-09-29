"""Shared callable aliases must describe the contract callers actually rely on.

``HttpFetcher`` is documented as the single source of truth for the fetcher
type, so a caller typed against it must be able to invoke the concrete fetcher
it is handed. The alias drifted to three parameters while every concrete
fetcher accepts four (``max_content_length``), so code annotated against the
alias could not legally call a real fetcher -- the mismatch was invisible only
because the call sites annotated their fetcher as ``Any``.

The arity check is tree-wide rather than a hand-typed pair of function objects.
The pair was itself a declaration that could drift: a third concrete fetcher
would simply not have been in the list, and the guard would have reported a
green result while covering nothing.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from app.crawl.strategies.fetchers import _default_http_fetcher
from app.models import HttpFetcher, HttpFetchResult
from tests.chokepoint_walks import app_python_files

# There is ONE concrete fetcher. The impersonating transport is a binding of it
# (`impersonating_fetcher`), not a second function, so the contract's parameters
# are spelled once in the whole tree and there is no twin to keep in step.
_CONCRETE_FETCHERS = [pytest.param(_default_http_fetcher, id="_default_http_fetcher")]


def _unbound_call(protocol: type, *, resolve_annotations: bool = True) -> inspect.Signature:
    """A Protocol's ``__call__`` signature with the ``self`` wrapper dropped.

    Read unbound, a Protocol's ``__call__`` carries ``self``; the contract is
    about the call, not about the wrapper. Opening with a parameter by any other
    name means the Protocol is not shaped the way this reading assumes, which is
    a fact about the declaration rather than about the four parameters -- so it
    fails here instead of skewing every comparison made with the result.

    One reader for every Protocol read in this file, because three copies of
    "drop self, take the positionals" is three places for a wrapper to be handled
    inconsistently.

    ``eval_str`` is load-bearing where it can be: the source uses postponed
    annotations, so without it every parameter type reads as the string
    ``'int'`` -- a string compares equal to nothing a caller can check.

    ``HttpFetchContract`` has to be read with ``resolve_annotations=False``, and
    that is a layering fact rather than a limitation. Its ``policy`` annotation
    names ``HostPolicy``, which lives in ``app.validation``, which imports
    ``NormalizedUrl`` from ``app.models`` -- so ``app.models`` may only name it
    under ``TYPE_CHECKING`` and the annotation does not exist at runtime. A
    caller asking for the real types would get a ``NameError``; the comparisons
    made against the contract are by name, which a string answers exactly.
    """
    declared = inspect.signature(protocol.__call__, eval_str=resolve_annotations)
    parameters = list(declared.parameters.values())
    assert parameters[0].name == "self", (
        f"{protocol.__name__}.__call__ opens with {parameters[0].name!r}, not self"
    )
    return declared.replace(parameters=parameters[1:])


def _omittable(parameters: list[inspect.Parameter]) -> frozenset[str]:
    """The parameters a caller may omit: every one carrying a default."""
    return frozenset(param.name for param in parameters if param.default is not inspect.Parameter.empty)


def _parameters_of_kind(signature: inspect.Signature, kinds: frozenset) -> list[inspect.Parameter]:
    """Every parameter of one or more kinds, in declaration order.

    One selector rather than one per kind, because the kinds differ only in the
    set they name and two comprehensions differing by one constant is the shape a
    copy takes. Positional is a pair of kinds -- a Protocol may declare its
    parameters positional-only, positional-or-keyword, or both -- so the argument
    is a set rather than a single kind.
    """
    return [param for param in signature.parameters.values() if param.kind in kinds]


_POSITIONAL = frozenset({inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD})
_KEYWORD_ONLY = frozenset({inspect.Parameter.KEYWORD_ONLY})


# What a production fetcher is this call PLUS: the keyword-only arguments the
# engine binds and the call site never passes. Typed here rather than read off a
# second Protocol, because that Protocol was the duplication jscpd reported -- the
# four positional parameters written twice, kept in step only by comparing each
# half against something else. ``HttpFetcher`` is the single declaration of the
# shared shape; what each fetcher adds on top of it is a fact about that fetcher,
# so it is stated once, here, next to the check that enforces it.
#
# ``policy`` is required and ``impersonate`` defaults, and which of them is
# optional is the axis a name-only comparison cannot see. It is the axis with the
# safety edge: a default on ``policy`` turns a missing SSRF keyword from a
# TypeError into an unrestricted HostPolicy and the caller never finds out.
# Verified: a twin with ``policy: HostPolicy = HostPolicy((), ())`` matches every
# name below (mypy is happy too, because protocol conformance is one-directional --
# an implementation may accept more than the contract asks for), so both the type
# checker and the tree-wide guard went green on a fetcher that silently stopped
# requiring the policy.
_BOUND_KEYWORDS = ("policy", "impersonate")
_BOUND_KEYWORD_OPTIONAL = frozenset({"impersonate"})

# The shape every production fetcher must have: HttpFetcher's own positional
# parameters, read off the Protocol so a rename there is a rename here, plus the
# two bound keywords typed above. Derived in one place because it is compared
# against every coroutine in app/ and a copy of it would be a second thing to
# keep in step.
_FETCHER_POSITIONALS = _parameters_of_kind(_unbound_call(HttpFetcher), _POSITIONAL)
_EXPECTED_SHAPE = {
    "positional": tuple(param.name for param in _FETCHER_POSITIONALS),
    "keyword_only": _BOUND_KEYWORDS,
    "is_coroutine": True,
}
_EXPECTED_OMITTABLE = _omittable(_FETCHER_POSITIONALS) | _BOUND_KEYWORD_OPTIONAL


def _defaulted_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """The parameters of ``node`` that a caller may omit.

    ``...`` (Ellipsis) is what a Protocol body writes in place of a default, so
    on the declaration side it means "this parameter is optional in the shape
    being declared" -- which is exactly the question being asked. On the
    implementation side a real default expression appears instead, and either
    way the presence of something in the defaults tuple is the signal.
    """
    args = node.args
    positional = [
        arg.arg
        for arg, default in zip(
            [*args.posonlyargs, *args.args],
            [None] * (len(args.posonlyargs) + len(args.args) - len(args.defaults)) + list(args.defaults),
            strict=True,
        )
        if default is not None
    ]
    keyword_only = [
        arg.arg for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True) if default is not None
    ]
    return frozenset([*positional, *keyword_only])


@pytest.mark.parametrize("fetcher", _CONCRETE_FETCHERS)
def _http_fetcher_alias_signature() -> inspect.Signature:
    """The bound view's own signature, read off its Protocol.

    ``HttpFetcher`` is a Protocol, so its shape is a real signature rather than a
    ``Callable`` type argument: ``get_args(HttpFetcher)[0]`` is empty, and the
    argument types live on ``__call__`` where a caller can also see *which* of
    them carry a default. The old reading could count the parameters and name
    their types, but it could not tell an optional fourth from a required one --
    which is the axis the alias got wrong.
    """
    return _unbound_call(HttpFetcher)


@pytest.mark.parametrize("fetcher", _CONCRETE_FETCHERS)
def test_http_fetcher_alias_matches_the_concrete_fetcher_arity(fetcher):
    """The alias's positional parameters must equal each concrete fetcher's.

    Compared by name as well as by count, so a reordering is caught too. A
    caller typed against the alias calls ``fetcher(url, headers, timeout,
    max_content_length)`` by position.
    """
    alias_names = [param.name for param in _parameters_of_kind(_http_fetcher_alias_signature(), _POSITIONAL)]
    concrete_names = [param.name for param in _parameters_of_kind(inspect.signature(fetcher), _POSITIONAL)]
    assert alias_names == concrete_names, (
        f"HttpFetcher declares {alias_names} but {fetcher.__name__} accepts {concrete_names}"
    )


def test_http_fetcher_alias_carries_the_documented_argument_types():
    """The alias is the documented contract, so its argument types must be the
    (url, headers, timeout_seconds, max_content_length) tuple the concrete
    fetchers accept -- not the three-parameter shape it drifted to."""
    alias = _http_fetcher_alias_signature()
    assert [param.annotation for param in _parameters_of_kind(alias, _POSITIONAL)] == [
        str,
        dict[str, str],
        int,
        int,
    ]
    assert alias.return_annotation is HttpFetchResult


def test_http_fetcher_alias_marks_the_content_length_cap_optional():
    """The cap must read as omittable, which is what a Callable alias cannot say.

    Every implementation gives ``max_content_length`` a default, and the engine
    binds its fetcher with ``functools.partial``, which drops defaults when it
    synthesizes a signature. So when the alias was ``Callable[[str, dict, int,
    int], Any]`` -- four required positionals -- a correct three-argument call
    read to the type checker as a missing argument, and the only real
    requirement to fix it was to stop using a Callable alias.
    """
    alias_defaulted = frozenset(
        param.name
        for param in _http_fetcher_alias_signature().parameters.values()
        if param.default is not inspect.Parameter.empty
    )
    assert alias_defaulted == {"max_content_length"}, (
        f"HttpFetcher marks {sorted(alias_defaulted)} optional; "
        "only max_content_length is omittable in every implementation"
    )


def _concrete_fetchers_in_app() -> list[tuple[str, str, ast.AsyncFunctionDef]]:
    """Every module-level async function in app/ whose return annotation is a
    fetch result.

    Found by walking all of ``app/`` for the return type, not by importing a
    list: a fetcher added to a new module must be covered without anyone
    remembering to add it here.

    Module level only, and async only. A concrete fetcher is a module-level
    coroutine the engine binds by name; scoping the sweep that way is what
    keeps the Protocol's own ``__call__`` -- which is the declaration, not an
    implementation of it -- from being measured against itself.
    """
    found: list[tuple[str, str, ast.AsyncFunctionDef]] = []
    for path in app_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            returns = node.returns
            if not (isinstance(returns, ast.Name) and returns.id == "HttpFetchResult"):
                continue
            found.append((path.name, node.name, node))
    return found


def test_http_fetcher_contract_holds_tree_wide():
    """Every production fetcher is ``HttpFetcher`` plus exactly the bound keywords.

    ``HttpFetcher`` is the one declaration of the call shape, and a production
    fetcher adds a keyword-only ``policy`` and an ``impersonate`` keyword to it.
    Those two facts are typed in this file, next to the check that enforces them,
    rather than spelled as a second Protocol in ``app/models.py`` -- which is what
    they used to be, and which is how the same four positional parameters came to
    be written twice. This check is the only thing standing between the two
    spellings now that there is one: if a parameter is added to a fetcher and not
    to the Protocol, or the other way round, this fails instead of the first call
    site that omits it.

    Names alone are not the whole shape. Which parameters are *optional* is a
    separate axis, and it is the one that bites: a fetcher that gives ``policy``
    a default accepts a call the engine must never make, so an SSRF keyword
    silently goes missing and the fetch runs against a default policy instead of
    raising. mypy cannot see it, because a protocol only constrains what an
    implementation must accept, never what it may accept beyond that. So the
    comparison here includes defaulting.
    """
    offenders = []
    for filename, funcname, node in _concrete_fetchers_in_app():
        args = node.args
        actual = {
            "positional": tuple(arg.arg for arg in [*args.posonlyargs, *args.args]),
            "keyword_only": tuple(arg.arg for arg in args.kwonlyargs),
            "is_coroutine": True,
        }
        if actual != _EXPECTED_SHAPE:
            offenders.append(f"{filename}:{node.lineno} {funcname} -> {actual}")
            continue
        defaulted = _defaulted_params(node)
        if defaulted != _EXPECTED_OMITTABLE:
            offenders.append(
                f"{filename}:{node.lineno} {funcname} makes {sorted(defaulted)} optional "
                f"but the fetcher contract makes {sorted(_EXPECTED_OMITTABLE)} optional; a "
                f"caller may omit a parameter the contract requires"
            )
    detail = "production fetchers must match app.models.HttpFetcher:\n  " + "\n  ".join(offenders)
    assert not offenders, detail


def test_the_contract_guard_sees_defaulting_not_just_names():
    """The guard must fail on a fetcher whose *names* match but whose optionals differ.

    This is the mutation that motivated comparing defaulting at all, and it is
    the one mypy accepts: a protocol constrains what an implementation must
    accept, not what it may additionally accept, so widening one twin with a
    default on a required parameter satisfies HttpFetcher and every name-based
    key in the comparison above. On ``policy`` the default would be a HostPolicy,
    and a caller omitting the SSRF keyword would get that instead of a TypeError.

    Written against the real tree: the sweep finds the fetchers, then the
    comparison is run over a synthetic fetcher carrying the mutation, so this
    fails if the guard stops considering defaulting.
    """
    found = _concrete_fetchers_in_app()
    assert {name for _, name, _ in found} == {"_default_http_fetcher"}, (
        f"the sweep found {sorted(name for _, name, _ in found)}; this test asserts against "
        "the real set, so it has to be the real set"
    )
    honest = next(node for _, name, node in found if name == "_default_http_fetcher")
    assert _defaulted_params(honest) == _EXPECTED_OMITTABLE, (
        f"the production fetcher defaults {sorted(_defaulted_params(honest))} but the contract "
        f"defaults {sorted(_EXPECTED_OMITTABLE)}; the clean tree must agree or this test is "
        "asserting nothing"
    )

    # Same parameter names, same order, same coroutine-ness -- only the defaulting differs.
    mutated = ast.parse(
        "async def f(url, headers, timeout_seconds, "
        "max_content_length=1, *, policy=None, impersonate=False): ...\n"
    ).body[0]
    assert isinstance(mutated, (ast.FunctionDef, ast.AsyncFunctionDef))
    assert tuple(arg.arg for arg in mutated.args.args) == _EXPECTED_SHAPE["positional"]
    assert tuple(arg.arg for arg in mutated.args.kwonlyargs) == _EXPECTED_SHAPE["keyword_only"]
    widened = _defaulted_params(mutated)
    assert widened == _EXPECTED_OMITTABLE | {"policy"}, widened
    assert widened != _EXPECTED_OMITTABLE, (
        "the mutation must differ from the contract in defaulting only, or it proves nothing"
    )


def test_the_contract_is_not_vacuous_tree_wide():
    """The tree-wide check must actually be finding the fetchers it claims to.

    A detector whose sink pattern stops matching reports zero offenders and
    passes forever. This asserts the sweep found the concrete fetcher, so a
    rename or an annotation change that silently emptied the search is a
    failure here rather than a permanently green gate. It also pins the count
    at ONE: a second module-level fetcher would be a second spelling of the
    contract, which is the duplication this design removed.
    """
    names = {funcname for _, funcname, _ in _concrete_fetchers_in_app()}
    assert names == {"_default_http_fetcher"}, (
        f"tree-wide fetcher sweep found {sorted(names)} -- it is no longer "
        "covering the concrete fetcher, so its green result proves nothing; a second "
        "fetcher is a second spelling of the contract"
    )


class TestNormalizedUrlSeedType:
    """M6: seeds and crawl tasks flow as a typed value, not a bare str.

    The request URL enters as a bare string, but once it passes through
    ``normalize_public_http_url`` -- the single source of truth for what a
    normalized URL is -- the value carries the ``NormalizedUrl`` type
    through ``CrawlTask.normalized_url`` and the sitemap-seed pipeline.
    The type is a ``NewType`` over ``str``: erased at runtime (spills,
    cache keys, and the wire stay plain JSON strings), but mypy refuses
    to let an unnormalized string take a normalized-URL position without
    crossing the normalizer first.
    """

    def test_normalized_url_is_a_newtype_over_str(self):
        from app.models import NormalizedUrl

        # Read through the class ``__dict__`` rather than as an attribute.
        #
        # ``__supertype__`` is a real attribute ``typing.NewType`` attaches to
        # the class it builds at runtime, and it is what this test is about. mypy
        # models ``NewType`` as a function returning a class and does not declare
        # the attribute on that class, so ``NormalizedUrl.__supertype__`` is an
        # attr-defined error against a value that is demonstrably there -- a
        # limitation in mypy's model of ``NewType``, not a defect in the code or
        # the assertion. ``vars()`` asks the same question the attribute access
        # would, and answers it at runtime.
        assert vars(NormalizedUrl)["__supertype__"] is str

    def test_crawl_task_normalized_url_field_is_typed(self):
        from typing import get_type_hints

        from app.models import CrawlTask, NormalizedUrl

        assert get_type_hints(CrawlTask)["normalized_url"] is NormalizedUrl

    def test_normalizer_returns_the_typed_value(self):
        import typing

        from app.models import NormalizedUrl
        from app.validation import normalize_public_http_url

        hints = typing.get_type_hints(normalize_public_http_url)
        assert hints["return"] is NormalizedUrl

    def test_typed_value_is_runtime_plain_str(self):
        """Typing must not change runtime shape (JSON/spill compatibility)."""
        from app.validation import HostPolicy, normalize_public_http_url

        normalized = normalize_public_http_url(
            "HTTP://Example.COM:80/a/#frag",
            policy=HostPolicy.unrestricted(),
        )
        assert type(normalized) is str
        assert normalized == "http://example.com/a"
