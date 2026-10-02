"""The one declaration of what a fetcher call looks like.

Four positional parameters: ``(url, headers, timeout_seconds,
max_content_length)``. The concrete fetcher accepts the content-length cap and
the retry wrapper passes it positionally, so the alias must describe four
parameters -- a caller typed against it must be able to invoke the real fetcher
it is handed.

A Protocol rather than a ``Callable[[str, dict[str, str], int, int], Any]``
alias, for two reasons that are both load-bearing. The fourth parameter is
optional in every implementation and a Callable alias cannot say so -- it types
all four as required -- and ``functools.partial`` drops the default when it
synthesizes a signature, so a correct three-argument call read as a missing
argument. The return is the result model, not ``Any``.

This is the ONLY declaration of the fetcher call shape. A second Protocol for
the same four parameters, plus a keyword-only ``policy`` and an ``impersonate``
keyword, used to sit here beside it -- the bound view and the concrete-fetcher
contract -- and Python cannot derive one from the other, because a function with
a *required* keyword-only argument does not satisfy a Protocol that does not
require it. So the four lines were written twice and could only be kept in step
by a guard that compared each half against a third thing.

The bound view is the right one to keep: it is the narrower declaration, and it
is the one a call site needs. The keywords a production fetcher adds on top of
it are not a second contract, they are a fact about each fetcher, and
``tests/test_type_contracts.py::test_http_fetcher_contract_holds_tree_wide``
checks them where they are declared: every coroutine in ``app/`` returning a
fetch result must take exactly the keyword-only ``policy`` (required) and
``impersonate``, with the same four positional parameters and the same
optionality. Both halves are load-bearing, and the defaulting half was found the
hard way: a fetcher that gave ``policy`` a default matched every name, because
protocol conformance is one-directional and a default changes no name -- so a
missing SSRF keyword would have become an unrestricted HostPolicy instead of a
TypeError, silently.
"""

from __future__ import annotations

from typing import Protocol

from app.models.http_fetch_result import HttpFetchResult


class HttpFetcher(Protocol):
    """The four positional parameters of a fetcher whose collaborators are already bound.

    Also the declaration of what a fetcher looks like *before* it is bound, which
    is the only sense in which this is a contract for the whole of ``app/``: a
    production fetcher is this call plus a keyword-only ``policy``, an
    ``impersonate`` keyword and a ``pin`` keyword, and the tree-wide check is
    what proves the "plus" is exactly those three.

    ``pin`` is the transport's pin rule, and ``None`` -- the default -- means the
    real one. It is declared here rather than kept off the contract because the
    guard compares defaulting as well as names, and a binding a contract cannot
    see is a binding that drifts. Its safety rests on that default: omitting it
    cannot substitute a weaker rule, so unlike ``policy`` it is optional in the
    harmless direction.
    """

    async def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout_seconds: int,
        max_content_length: int = ...,
    ) -> HttpFetchResult: ...
