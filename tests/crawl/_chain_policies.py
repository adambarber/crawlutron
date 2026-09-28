"""The one strategy-chain policy these crawl-side chain scenarios hand the chain.

``execute_crawl_chain`` takes a whole policy, and every scenario here needs the
same three things out of it: a ladder, a limits block that does not consult
robots, and no request profile. Writing that per scenario meant the limits
block existed more than once with a chance of drifting, and the copy that
drifted is the one the test runs.
"""

from tests._policy_factories import crawl_limits


def chain_policy(*strategies: str, discovery: dict | None = None) -> dict:
    """A chain policy over `strategies`.

    The limits are :func:`_policy_factories.crawl_limits` with robots explicitly
    off, rather than a second copy of those three numbers. A chain scenario is
    about the ladder, and a robots lookup would add a network hop that says
    nothing about which step ran -- but the timeout, the retry count and the link
    ceiling are the same profile every other crawl fixture uses, and having it
    here too is how the two drifted apart.

    A fresh ``limits`` copy per call, because callers are told to mutate the
    policy they are handed. ``discovery`` defaults to "do not consult robots";
    a scenario that asserts on the robots diagnostics asks for them by name.
    """
    return {
        "strategies": list(strategies),
        "limits": crawl_limits(respect_robots=False),
        "headers": {},
        "discovery": {"robots": False} if discovery is None else discovery,
    }


__all__ = ["chain_policy"]
