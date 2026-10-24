"""The one strategy-chain policy these crawl-side chain scenarios hand the chain.

``execute_crawl_chain`` takes a whole policy; every scenario needs the same
three things -- a ladder, no-robots limits, no profile -- so it is written once.
"""

from tests._policy_factories import crawl_limits


def chain_policy(*strategies: str, discovery: dict[str, object] | None = None) -> dict[str, object]:
    """A chain policy over `strategies`.

    The limits are ``crawl_limits`` with robots off (no second copy to drift),
    a fresh ``limits`` per call (callers mutate it), and ``discovery`` off unless asked.
    """
    return {
        "strategies": list(strategies),
        "limits": crawl_limits(respect_robots=False),
        "headers": {},
        "discovery": {"robots": False} if discovery is None else discovery,
    }


__all__ = ["chain_policy"]
