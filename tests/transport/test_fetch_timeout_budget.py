"""The fetch door's timeout is a seconds budget a caller may state in fractions."""

from __future__ import annotations

from crawlutron.transport import fetchers as fetchers_mod
from crawlutron.transport.fetchers import default_http_fetcher
from tests._fetch_doubles import make_fetch
from tests._fetcher_factories import ScriptedTransport, allow_scripted_egress
from tests._policy_factories import canonical


def _install_budget_transport(monkeypatch) -> ScriptedTransport:
    """Point the fetcher at a transport that records every hop's FetchRequest.

    The transport is the fetch wire: ``FetchRequest.timeout_seconds`` is the value
    a hop is dialed with, so recording it pins what the caller's budget became.
    """
    transport = ScriptedTransport([make_fetch(status=200, body=b"ok")])
    monkeypatch.setattr(fetchers_mod, "HttpxTransport", lambda settings=None: transport)
    allow_scripted_egress(monkeypatch)
    return transport


def _dialed_timeouts(transport: ScriptedTransport) -> list[float]:
    """Every hop's timeout budget, in the order the hops were built."""
    return [request.timeout_seconds for request, _max_bytes in transport.calls]


async def test_sub_second_timeout_reaches_the_fetch_unaltered(monkeypatch) -> None:
    """A fractional budget arrives at the fetch as the float it was given.

    The walk-wide deadline makes the dialed budget a float remainder; 0.5
    must not be coerced to int 0.
    """
    transport = _install_budget_transport(monkeypatch)

    await default_http_fetcher(canonical("http://example.test/"), {}, 0.5)

    dialed = _dialed_timeouts(transport)
    assert abs(dialed[0] - 0.5) < 0.01, dialed


async def test_first_hop_receives_remaining_wall_budget(monkeypatch) -> None:
    """The first hop receives the remaining wall budget (float <= stated).

    The dialed budget is always a float remainder, never the raw int caller
    value; there is no int-budget path to preserve.
    """
    transport = _install_budget_transport(monkeypatch)

    await default_http_fetcher(canonical("http://example.test/"), {}, 10)

    dialed = _dialed_timeouts(transport)
    assert dialed[0] <= 10.0, dialed
    assert isinstance(dialed[0], float), dialed
