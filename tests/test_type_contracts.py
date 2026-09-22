"""Shared callable aliases must describe the contract callers actually rely on.

``HttpFetcher`` is documented as the single source of truth for the fetcher
type, so a caller typed against it must be able to invoke the concrete fetcher
it is handed. The alias drifted to three parameters while every concrete
fetcher accepts four (``max_content_length``), so code annotated against the
alias could not legally call a real fetcher -- the mismatch was invisible only
because the call sites annotated their fetcher as ``Any``.
"""

from __future__ import annotations

import inspect
from typing import get_args

import pytest

from app.crawl.strategies.http import _default_cffi_fetcher, _default_http_fetcher
from app.models import HttpFetcher

_CONCRETE_FETCHERS = [
    pytest.param(_default_http_fetcher, id="_default_http_fetcher"),
    pytest.param(_default_cffi_fetcher, id="_default_cffi_fetcher"),
]


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
        from app.validation import normalize_public_http_url

        normalized = normalize_public_http_url("HTTP://Example.COM:80/a/#frag")
        assert type(normalized) is str
        assert normalized == "http://example.com/a"
