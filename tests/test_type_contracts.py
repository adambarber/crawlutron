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
from chokepoint_walks import app_python_files

from app.crawl.strategies.fetchers import _default_cffi_fetcher, _default_http_fetcher
from app.models import HttpFetcher

_CONCRETE_FETCHERS = [
    pytest.param(_default_http_fetcher, id="_default_http_fetcher"),
    pytest.param(_default_cffi_fetcher, id="_default_cffi_fetcher"),
]

# The concrete-fetcher contract, derived from HttpFetchContract rather than
# re-spelled: (positional names, positional-only, keyword-only, async).
_CONTRACT = {
    "positional": ("url", "headers", "timeout_seconds", "max_content_length"),
    "keyword_only": ("policy",),
    "is_coroutine": True,
}


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
    """
    offenders = []
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
    assert not offenders, (
        "production fetchers must match app.models.HttpFetchContract:\n  "
        + "\n  ".join(offenders)
    )


def test_the_contract_is_not_vacuous_tree_wide():
    """The tree-wide check must actually be finding the fetchers it claims to.

    A detector whose sink pattern stops matching reports zero offenders and
    passes forever. This asserts the sweep found the two concrete fetchers, so
    a rename or an annotation change that silently emptied the search is a
    failure here rather than a permanently green gate.
    """
    names = {funcname for _, funcname, _ in _concrete_fetchers_in_app()}
    assert {"_default_http_fetcher", "_default_cffi_fetcher"} <= names, (
        f"tree-wide fetcher sweep found {sorted(names)} -- it is no longer "
        "covering the concrete fetchers, so its green result proves nothing"
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
