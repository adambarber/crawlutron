"""The HTTP fetcher must offload blocking DNS resolution
to a worker thread so the event loop is never stalled by a resolver call."""

import threading

import httpx

from crawlutron.transport import pinned_transport
from crawlutron.transport.fetchers import default_http_fetcher
from tests._policy_factories import canonical


def _thread_recording_spy(seen: dict[str, threading.Thread], *, dead_port: bool = True):
    """A pin rule that records the thread it ran on.

    Both tests assert the same property -- the rule ran off the loop thread --
    so the recorder is shared and passed as ``pin=``, not by module swap.
    """
    from crawlutron.transport.pinned_transport import PinnedRequest

    def spy(url, headers):
        seen["thread"] = threading.current_thread()
        connect = "http://127.0.0.1:9/" if dead_port else url.raw
        return PinnedRequest(connect_url=connect, headers=dict(headers), url=url)

    return spy


async def test_http_fetcher_offloads_the_pin_rule_to_thread():
    """The pin rule (which performs a blocking socket.getaddrinfo) must run in a
    worker thread, not the event loop thread -- and the fetcher must actually
    pass one through, rather than building its own and ignoring the argument."""
    seen: dict[str, threading.Thread] = {}

    # The spy redirects the connection at a closed loopback port; whether the
    # fetch raises or returns a gateway error, it must not come back a page.
    outcome = await _dead_port_fetch_outcome(seen)
    if outcome is not None:
        assert outcome.status_code >= 400, outcome.status_code

    assert seen, "the pin rule was never invoked"
    # The spy ran in a dedicated DNS worker thread, not the asyncio event loop.
    assert seen["thread"] is not threading.current_thread()
    # On THIS module's pool specifically, not merely off the loop: the pool is sized so concurrent resolution
    # does not contend with the default executor that blocking extraction runs on, and any other worker would
    assert seen["thread"].name.startswith("dns-resolve")


# The closed-port fetch job shared by the test above (single use, kept next to
# its caller so the dead-port answer rots in one place, not across the file).
async def _dead_port_fetch_outcome(seen: dict[str, threading.Thread]):
    try:
        return await default_http_fetcher(
            canonical("http://example.com/"),
            {},
            timeout_seconds=2,
            pin=_thread_recording_spy(seen),
        )
    except (httpx.ConnectError, httpx.TimeoutException):
        return None


async def test_pinned_hop_offloads_the_pin_rule_to_thread():
    """The redirect walk off-loads whatever rule it was given.

    Same property as the fetcher test, at the walk; this also pins that the
    hop yields the rule's connect-ready URL/headers, not the rule's input.
    """
    seen: dict[str, threading.Thread] = {}

    walk = pinned_transport.apinned_hops(
        canonical("http://example.com/"),
        {},
        pin=_thread_recording_spy(seen, dead_port=False),
    )
    chain, pinned = await anext(walk)

    assert seen, "the pin rule was never invoked"
    assert seen["thread"] is not threading.current_thread()
    assert seen["thread"].name.startswith("dns-resolve")
    assert pinned.connect_url == "http://example.com/"
    assert pinned.headers == {}
    assert chain.url == canonical("http://example.com/")
