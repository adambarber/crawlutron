"""The one strategy-chain policy these crawl-side chain scenarios hand the chain.

``execute_crawl_chain`` takes a whole policy, and every scenario here needs the
same three things out of it: a ladder, a limits block that does not consult
robots, and no request profile. Writing that per scenario meant the limits
block existed more than once with a chance of drifting, and the copy that
drifted is the one the test runs.
"""

#: Ten seconds, no retries, little room for discovered links, robots explicitly
#: off. A chain scenario is about the ladder; a robots lookup would add a
#: network hop that says nothing about which step ran.
CHAIN_LIMITS = {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10, "respect_robots": False}


def chain_policy(*strategies: str, discovery: dict | None = None) -> dict:
    """A chain policy over `strategies`.

    A fresh ``limits`` copy per call, because callers are told to mutate the
    policy they are handed. ``discovery`` defaults to "do not consult robots";
    a scenario that asserts on the robots diagnostics asks for them by name.
    """
    return {
        "strategies": list(strategies),
        "limits": dict(CHAIN_LIMITS),
        "headers": {},
        "discovery": {"robots": False} if discovery is None else discovery,
    }


__all__ = ["CHAIN_LIMITS", "chain_policy"]
