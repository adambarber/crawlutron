"""The HTTP fetcher must offload blocking DNS resolution
to a worker thread so the event loop is never stalled by a resolver call."""

import threading

import httpx
import pytest

from app.transport import pinned_transport
from app.transport.fetchers import default_http_fetcher
from tests._policy_factories import canonical


def _thread_recording_spy(seen: dict, *, dead_port: bool = True):
    """A pin rule that records the thread it ran on.

    Both tests below assert one property -- the rule ran off the event-loop
    thread -- so the recorder is one thing they share. Only what the spy hands
    back differs: the fetcher test points the connection at a closed loopback
    port so the fetch fails fast without real network work, and the walk test
    echoes the caller's own URL back so it can assert the yield.

    It is passed as `pin=` rather than rebound over the module's builder, which
    is the whole point of the seam: off-loading is a property of the call site,
    so it must be checkable with a rule that is not the module's own.
    """

    def spy(url, headers):
        seen["thread"] = threading.current_thread()
        return (("http://127.0.0.1:9/" if dead_port else url.raw), dict(headers))

    return spy


async def test_http_fetcher_offloads_the_pin_rule_to_thread():
    """The pin rule (which performs a blocking socket.getaddrinfo) must run in a
    worker thread, not the event loop thread -- and the fetcher must actually
    pass one through, rather than building its own and ignoring the argument."""
    seen: dict = {}

    # The spy redirects the connection at a closed loopback port, so the fetch reaches the transport and fails
    # to connect.
    with pytest.raises(httpx.ConnectError):
        await default_http_fetcher(
            canonical("http://example.com/"),
            {},
            timeout_seconds=2,
            pin=_thread_recording_spy(seen),
        )

    assert seen, "the pin rule was never invoked"
    # The spy ran in a dedicated DNS worker thread, not the asyncio event loop.
    assert seen["thread"] is not threading.current_thread()
    # On THIS module's pool specifically, not merely off the loop: the pool is sized so concurrent resolution
    # does not contend with the default executor that blocking extraction runs on, and any other worker would
    assert seen["thread"].name.startswith("dns-resolve")


async def test_pinned_hop_offloads_the_pin_rule_to_thread():
    """The redirect walk off-loads whatever rule it was given.

    Same property as the fetcher test, at the walk instead of the transport,
    and this one also pins down what the walk yields: the rule's connect-ready
    URL and headers reach the caller of the hop, not the rule's input.
    """
    seen: dict = {}

    walk = pinned_transport.apinned_hops(
        canonical("http://example.com/"),
        {},
        pin=_thread_recording_spy(seen, dead_port=False),
    )
    chain, pinned_url, hop_headers = await anext(walk)

    assert seen, "the pin rule was never invoked"
    assert seen["thread"] is not threading.current_thread()
    assert seen["thread"].name.startswith("dns-resolve")
    assert pinned_url == "http://example.com"
    assert hop_headers == {}
    assert chain.url == canonical("http://example.com/")
