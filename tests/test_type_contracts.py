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
from typing import get_args

import pytest
from tests.chokepoint_walks import app_python_files

from app.crawl.strategies.fetchers import _default_http_fetcher
from app.models import HttpFetchContract, HttpFetcher

# There is ONE concrete fetcher. The impersonating transport is a binding of it
# (`impersonating_fetcher`), not a second function, so the contract's parameters
# are spelled once in the whole tree and there is no twin to keep in step.
_CONCRETE_FETCHERS = [pytest.param(_default_http_fetcher, id="_default_http_fetcher")]


def _contract_signature() -> dict[str, object]:
    """The contract's own shape, read off ``HttpFetchContract``.

    Derived, not typed. A tuple written here is a second copy of the Protocol
    whose only reviewer is the comparison in this same file, so the copy went
    stale the moment ``impersonate`` was added to the contract and every
    fetcher check below went red for a reason that had nothing to do with a
    fetcher. Reading the Protocol means a keyword is guarded the day it is
    added.
    """
    parameters = list(inspect.signature(HttpFetchContract.__call__).parameters.values())
    if parameters and parameters[0].name == "self":
        # Read as an unbound function, the protocol's __call__ carries `self`;
        # the contract is about the call, not about the wrapper.
        parameters = parameters[1:]
    positional = tuple(
        param.name
        for param in parameters
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    )
    keyword_only = tuple(param.name for param in parameters if param.kind is inspect.Parameter.KEYWORD_ONLY)
    return {
        "positional": positional,
        "keyword_only": keyword_only,
        "is_coroutine": inspect.iscoroutinefunction(HttpFetchContract.__call__),
    }


_CONTRACT = _contract_signature()

# Which parameters HttpFetchContract gives a default to, read off the Protocol
# rather than typed here. This is the axis the name comparison above cannot see,
# and it is the one with a safety edge: a default on `policy` turns a missing
# SSRF keyword from a TypeError into an unrestricted HostPolicy, and the caller
# never finds out. A name-only comparison passes that mutation -- verified: a
# twin with `policy: HostPolicy = HostPolicy((), ())` satisfies HttpFetchContract
# (mypy is happy, because protocol conformance is one-directional: an
# implementation may accept more than the contract asks for) and matches every
# key in _CONTRACT, so both the type checker and the tree-wide guard below went
# green on a fetcher that silently stopped requiring the policy.
_CONTRACT_DEFAULTED = frozenset(
    name
    for name, param in inspect.signature(HttpFetchContract.__call__).parameters.items()
    if param.default is not inspect.Parameter.empty
)


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
def test_http_fetcher_alias_matches_the_concrete_fetcher_arity(fetcher):
    """The alias's positional arity must equal each concrete fetcher's.

    ``Callable[[...args], ReturnType]`` carries its argument types as the first
    element of ``get_args`` (the second is the return type), so comparing that
    list to the concrete signature's positional parameters catches exactly the
    drift the alias was written to prevent: a caller typed against the alias
    calling ``fetcher(url, headers, timeout, max_content_length)``.
    """
    alias_args = get_args(HttpFetcher)[0]
    signature = inspect.signature(fetcher)
    positional = [
        param
        for param in signature.parameters.values()
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD)
    ]
    assert len(alias_args) == len(positional), (
        f"HttpFetcher declares {len(alias_args)} positional parameters "
        f"{alias_args} but {fetcher.__name__} accepts "
        f"{[param.name for param in positional]}"
    )


def test_http_fetcher_alias_carries_the_documented_argument_types():
    """The alias is the documented contract, so its argument types must be the
    (url, headers, timeout_seconds, max_content_length) tuple the concrete
    fetchers accept -- not the three-parameter shape it drifted to."""
    assert get_args(HttpFetcher)[0] == [str, dict[str, str], int, int]


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
    """Every production fetcher matches HttpFetchContract, wherever it lives.

    ``HttpFetchContract`` states the shape once precisely because the two
    concrete fetchers cannot share a signature -- Python has no way to say it
    once for two entry points the engine hands out separately. That makes this
    check the only thing standing between the copy and silent drift: if a
    parameter is added to one fetcher and not the other, this fails instead of
    the first call site that omits it.

    Names alone are not the whole shape. Which parameters are *optional* is a
    separate axis, and it is the one that bites: a fetcher that gives ``policy``
    a default accepts a call the contract forbids, so an SSRF keyword silently
    goes missing and the fetch runs against a default policy instead of raising.
    mypy cannot see it, because a protocol only constrains what an
    implementation must accept, never what it may accept beyond that. So the
    comparison here includes defaulting, taken from the Protocol itself.
    """
    offenders = []
    contract_defaulted = _CONTRACT_DEFAULTED
    for filename, funcname, node in _concrete_fetchers_in_app():
        args = node.args
        positional = tuple(arg.arg for arg in [*args.posonlyargs, *args.args])
        keyword_only = tuple(arg.arg for arg in args.kwonlyargs)
        actual = {
            "positional": positional,
            "keyword_only": keyword_only,
            "is_coroutine": True,
        }
        if actual != _CONTRACT:
            offenders.append(f"{filename}:{node.lineno} {funcname} -> {actual}")
            continue
        defaulted = _defaulted_params(node)
        if defaulted != contract_defaulted:
            offenders.append(
                f"{filename}:{node.lineno} {funcname} makes {sorted(defaulted)} optional "
                f"but HttpFetchContract makes {sorted(contract_defaulted)} optional; a caller "
                f"may omit a parameter the contract requires"
            )
    assert not offenders, "production fetchers must match app.models.HttpFetchContract:\n  " + "\n  ".join(
        offenders
    )


def test_the_contract_guard_sees_defaulting_not_just_names():
    """The guard must fail on a fetcher whose *names* match but whose optionals differ.

    This is the mutation that motivated comparing defaulting at all, and it is
    the one mypy accepts: a protocol constrains what an implementation must
    accept, not what it may additionally accept, so widening one twin with a
    default on a required parameter satisfies HttpFetchContract and every
    name-based key in the comparison above. On ``policy`` the default would be
    a HostPolicy, and a caller omitting the SSRF keyword would get that instead
    of a TypeError.

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
    assert _defaulted_params(honest) == _CONTRACT_DEFAULTED, (
        f"the production fetcher defaults {sorted(_defaulted_params(honest))} but the contract "
        f"defaults {sorted(_CONTRACT_DEFAULTED)}; the clean tree must agree or this test is "
        "asserting nothing"
    )

    # Same parameter names, same order, same coroutine-ness -- only the defaulting differs.
    mutated = ast.parse(
        "async def f(url, headers, timeout_seconds, "
        "max_content_length=1, *, policy=None, impersonate=False): ...\n"
    ).body[0]
    assert tuple(arg.arg for arg in mutated.args.args) == _CONTRACT["positional"]
    assert tuple(arg.arg for arg in mutated.args.kwonlyargs) == _CONTRACT["keyword_only"]
    widened = _defaulted_params(mutated)
    assert widened == _CONTRACT_DEFAULTED | {"policy"}, widened
    assert widened != _CONTRACT_DEFAULTED, (
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

        assert NormalizedUrl.__supertype__ is str

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
