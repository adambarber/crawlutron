"""Shared callable aliases must describe the contract callers rely on.

``HttpFetcher`` drifted to three parameters while every concrete fetcher
accepts four; the arity check is a tree-wide walk, not a hand-typed pair."""

from __future__ import annotations

import ast
import inspect
from typing import cast

import pytest

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.http_fetcher import HttpFetcher
from crawlutron.transport.fetchers import default_http_fetcher
from tests._policy_factories import canonical
from tests._result_builders import _link
from tests.chokepoint_walks import (
    app_python_files,
    calls_named_leaf,
    resolved_call_name,
    resolved_expr_name,
)

# There is ONE concrete fetcher.
_CONCRETE_FETCHERS = [pytest.param(default_http_fetcher, id="default_http_fetcher")]


def _unbound_call(protocol: type, *, resolve_annotations: bool = True) -> inspect.Signature:
    """A Protocol's ``__call__`` signature with the ``self`` wrapper dropped.

    One reader for every Protocol here, so a wrapper is never handled
    inconsistently; ``eval_str`` is load-bearing for postponed annotations."""
    declared = inspect.signature(protocol.__call__, eval_str=resolve_annotations)
    parameters = list(declared.parameters.values())
    assert parameters[0].name == "self", (
        f"{protocol.__name__}.__call__ opens with {parameters[0].name!r}, not self"
    )
    return declared.replace(parameters=parameters[1:])


def _omittable(parameters: list[inspect.Parameter]) -> frozenset[str]:
    """The parameters a caller may omit: every one carrying a default."""
    return frozenset(param.name for param in parameters if param.default is not inspect.Parameter.empty)


def _parameters_of_kind(
    signature: inspect.Signature, kinds: frozenset[inspect._ParameterKind]
) -> list[inspect.Parameter]:
    """Every parameter of one or more kinds, in declaration order.

    One selector rather than one per kind; positional is a pair of kinds, so
    the argument is a set."""
    return [param for param in signature.parameters.values() if param.kind in kinds]


_POSITIONAL = frozenset({inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD})
_KEYWORD_ONLY = frozenset({inspect.Parameter.KEYWORD_ONLY})


# What a production fetcher is this call PLUS: the keyword-only arguments the engine binds and the call site
# never passes.
_BOUND_KEYWORDS = ("impersonate", "pin", "release_stream_noblock")
# ``pin`` is optional for safety-edge reasons; ``release_stream_noblock`` is the borrow-close
# injection seam callers omit in production (they get the private non-blocking closer).
_BOUND_KEYWORD_OPTIONAL = frozenset({"impersonate", "pin", "release_stream_noblock"})

# The shape every production fetcher must have: HttpFetcher's own positional parameters, read off the Protocol
# so a rename there is a rename here, plus the two bound keywords typed above.
_FETCHER_POSITIONALS = _parameters_of_kind(_unbound_call(HttpFetcher), _POSITIONAL)
_EXPECTED_SHAPE = {
    "positional": tuple(param.name for param in _FETCHER_POSITIONALS),
    "keyword_only": _BOUND_KEYWORDS,
    "is_coroutine": True,
}
_EXPECTED_OMITTABLE = _omittable(_FETCHER_POSITIONALS) | _BOUND_KEYWORD_OPTIONAL


def _defaulted_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    """The parameters of ``node`` that a caller may omit.

    ``...`` on the declaration side and a real default on the implementation
    side both mean "omittable"; either way, presence in the tuple is the signal."""
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

    ``HttpFetcher``'s shape is a real signature: argument types live on
    ``__call__``, where a caller also sees which carry a default."""
    return _unbound_call(HttpFetcher)


@pytest.mark.parametrize("fetcher", _CONCRETE_FETCHERS)
def test_http_fetcher_alias_matches_the_concrete_fetcher_arity(fetcher):
    """The alias's positional parameters must equal each concrete fetcher's.

    Compared by name as well as count, so a reordering is caught; callers invoke
    the fetcher by position."""
    alias_names = [param.name for param in _parameters_of_kind(_http_fetcher_alias_signature(), _POSITIONAL)]
    concrete_names = [param.name for param in _parameters_of_kind(inspect.signature(fetcher), _POSITIONAL)]
    assert alias_names == concrete_names, (
        f"HttpFetcher declares {alias_names} but {fetcher.__name__} accepts {concrete_names}"
    )


def test_http_fetcher_alias_carries_the_documented_argument_types():
    """The alias is the documented contract, so its argument types must be the
    (url, headers, timeout_seconds, max_content_length) tuple the concrete
    fetchers accept -- not the three-parameter shape it drifted to."""
    from safe_url.normalized_url import NormalizedUrl

    alias = _http_fetcher_alias_signature()
    assert [param.annotation for param in _parameters_of_kind(alias, _POSITIONAL)] == [
        NormalizedUrl,
        dict[str, str],
        int,
        int,
    ]
    assert alias.return_annotation is HttpFetchResult


def test_http_fetcher_alias_marks_the_content_length_cap_optional():
    """The cap must read as omittable, which a Callable alias cannot say.

    ``functools.partial`` drops defaults, so a four-required Callable read a
    correct three-argument call as a missing one."""
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
    """Every module-level async function in app/ whose return is a fetch result.

    Found by walking ``app/``: a new fetcher is covered unnamed; the Protocol's
    own ``__call__`` escapes; tail helpers returning a result are not fetchers."""
    found: list[tuple[str, str, ast.AsyncFunctionDef]] = []
    for path in app_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            returns = node.returns
            if not (isinstance(returns, ast.Name) and returns.id == "HttpFetchResult"):
                continue
            first_args = [*node.args.posonlyargs, *node.args.args]
            if not first_args:
                continue
            first = first_args[0].annotation
            if isinstance(first, ast.Name) and first.id == "HttpFetcher":
                continue
            found.append((path.name, node.name, node))
    return found


def test_http_fetcher_contract_holds_tree_wide():
    """Every production fetcher is ``HttpFetcher`` plus exactly the bound keywords.

    Typed beside the check, not as a second Protocol; the comparison covers
    defaulting, which a protocol never constrains on an implementation."""
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
    detail = "production fetchers must match crawlutron.models.HttpFetcher:\n  " + "\n  ".join(offenders)
    assert not offenders, detail


def test_the_contract_guard_sees_defaulting_not_just_names():
    """Fail on a fetcher whose *names* match but whose optionals differ.

    The mutation mypy accepts: defaulting a required parameter satisfies the
    Protocol, so the widened synthetic fetcher exercises the defaulting axis."""
    found = _concrete_fetchers_in_app()
    assert {name for _, name, _ in found} == {"default_http_fetcher"}, (
        f"the sweep found {sorted(name for _, name, _ in found)}; this test asserts against "
        "the real set, so it has to be the real set"
    )
    honest = next(node for _, name, node in found if name == "default_http_fetcher")
    assert _defaulted_params(honest) == _EXPECTED_OMITTABLE, (
        f"the production fetcher defaults {sorted(_defaulted_params(honest))} but the contract "
        f"defaults {sorted(_EXPECTED_OMITTABLE)}; the clean tree must agree or this test is "
        "asserting nothing"
    )

    # Same parameter names, same order, same coroutine-ness -- only the defaulting differs.
    kwonly = cast("tuple[str, ...]", _EXPECTED_SHAPE["keyword_only"])
    rest = ", ".join(f"{name}=None" for name in kwonly)
    mutated_tree = ast.parse(
        f"async def f(url, headers, timeout_seconds=10, max_content_length=1, *, {rest}): ...\n"
    )
    mutated_node = next(node for node in mutated_tree.body if isinstance(node, ast.AsyncFunctionDef))
    mutated = mutated_node
    assert tuple(arg.arg for arg in mutated.args.args) == _EXPECTED_SHAPE["positional"]
    assert tuple(arg.arg for arg in mutated.args.kwonlyargs) == _EXPECTED_SHAPE["keyword_only"]
    widened = _defaulted_params(mutated)
    assert widened == _EXPECTED_OMITTABLE | {"timeout_seconds"}, widened
    assert widened != _EXPECTED_OMITTABLE, (
        "the mutation must differ from the contract in defaulting only, or it proves nothing"
    )


def test_production_never_substitutes_the_pin_rule():
    """``pin=`` is a test door, and a production call site must never use it.

    A substituted pin rule skips DNS resolution and the SSRF check it feeds;
    a walk because that failure is silent by construction."""
    offenders: list[str] = []
    for path in app_python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for call in calls_named_leaf(tree, {"default_http_fetcher", "partial"}):
            if not _binds_fetcher(call, tree):
                continue
            for keyword in call.keywords:
                if keyword.arg == "pin":
                    offenders.append(f"{path.name}:{call.lineno}")
    assert not offenders, (
        f"production substitutes the pin rule at {', '.join(offenders)}; a substituted rule "
        "skips the SSRF resolution the default performs. If a module genuinely needs a "
        "different rule, that is a real seam to design, not a keyword to pass"
    )


def _binds_fetcher(call: ast.Call, tree: ast.Module) -> bool:
    """Whether ``call`` produces a fetcher -- directly, or by partial-binding one.

    The engine hands the strategy a ``functools.partial``, so a check that only
    recognized the bare call would walk past every production site."""
    if resolved_call_name(tree, call) == "default_http_fetcher":
        return True
    call_func = call.func
    if not isinstance(call_func, ast.Call):
        return False
    return (
        bool(call.args)
        and resolved_call_name(tree, call_func) in {"partial", "functools.partial"}
        and resolved_expr_name(tree, call.args[0]) == "default_http_fetcher"
    )


def test_the_contract_is_not_vacuous_tree_wide():
    """The tree-wide check must actually find the fetchers it claims to.

    A detector whose sink stops matching passes forever; the count is pinned at
    ONE so an emptied sweep fails here."""
    names = {funcname for _, funcname, _ in _concrete_fetchers_in_app()}
    assert names == {"default_http_fetcher"}, (
        f"tree-wide fetcher sweep found {sorted(names)} -- it is no longer "
        "covering the concrete fetcher, so its green result proves nothing; a second "
        "fetcher is a second spelling of the contract"
    )


class TestNormalizedUrlIsAValueNotAString:
    """A URL that crossed admission is a VALUE; text is something it lends.

    ``NormalizedUrl`` was a ``NewType`` over ``str`` (every position accepted
    concatenation unchecked); it is now a frozen value lending text at the edge."""

    def test_the_type_is_a_value_not_a_newtype_over_str(self):
        from safe_url.normalized_url import NormalizedUrl

        # The attribute a ``NewType`` carries at runtime is what a reader looks for first; its absence is the
        # design.
        assert "__supertype__" not in vars(NormalizedUrl)
        assert not issubclass(NormalizedUrl, str)

    def test_the_admission_boundary_is_typed_as_the_value(self):
        from typing import get_type_hints

        from safe_url.normalized_url import NormalizedUrl

        from crawlutron.admission.admission_links import AdmittedLink

        assert get_type_hints(AdmittedLink)["normalized_url"] is NormalizedUrl

    def test_records_carry_the_value_and_the_wire_carries_text(self):
        from typing import get_type_hints

        from safe_url.normalized_url import NormalizedUrl

        from crawlutron.models.crawl_task import CrawlTask
        from crawlutron.models.discovered_link import DiscoveredLink
        from crawlutron.models.serialization import to_dict

        # Both cross a boundary: ``DiscoveredLink`` is rebuilt from the spill file, and ``CrawlTask`` is
        # persisted.
        assert get_type_hints(DiscoveredLink)["normalized_url"] is NormalizedUrl
        assert get_type_hints(CrawlTask)["normalized_url"] is NormalizedUrl
        task = CrawlTask(
            index=0,
            original_url="http://a.test/x",
            normalized_url=canonical("http://a.test/x"),
        )
        link = _link("http://a.test/x", "http://a.test/")
        assert isinstance(to_dict(task)["normalized_url"], str)
        assert isinstance(to_dict(link)["normalized_url"], str)

    def test_the_constructor_is_the_admission_decision(self, monkeypatch):
        from safe_url.host_policy import HostPolicy
        from safe_url.normalized_url import NormalizedUrl

        monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
        policy = HostPolicy.unrestricted()
        # A host the policy refuses has no value: there is no way to hold an unadmitted URL in this type at
        # all.
        with pytest.raises(ValueError, match="private_network_target"):
            NormalizedUrl("http://localhost/x", policy=policy)
        with pytest.raises(ValueError, match="unsupported_scheme"):
            NormalizedUrl("file:///etc/passwd", policy=policy)

    def test_host_and_site_are_derived_once_at_construction(self):
        from safe_url.host_policy import HostPolicy
        from safe_url.normalized_url import NormalizedUrl

        value = NormalizedUrl("https://Deep.Sub.Example.co.uk:443/p/", policy=HostPolicy.unrestricted())
        assert value.raw == "https://deep.sub.example.co.uk/p/"  # #520: path text survives verbatim
        assert value.host == "deep.sub.example.co.uk"
        assert value.site == "example.co.uk"

    def test_equal_canonical_strings_are_equal_values(self):
        from safe_url.host_policy import HostPolicy
        from safe_url.normalized_url import NormalizedUrl

        policy = HostPolicy.unrestricted()
        # #520: path text is identity, so slash-equivalence is no longer normalized away;
        # tracking-param dropping still canonicalizes to one value.
        one = NormalizedUrl("https://example.com/a?utm_source=x", policy=policy)
        two = NormalizedUrl("https://example.com/a", policy=policy)
        assert one == two
        assert len({one, two}) == 1
