"""The one strategy-chain policy these crawl-side chain scenarios hand the chain.

``execute_crawl_chain`` takes a whole policy; every scenario needs the same
three things -- a ladder, no-robots limits, no profile -- so it is written once.
"""

from crawlutron.models.json_value import JsonValue
from tests._policy_factories import crawl_limits


def chain_policy(*strategies: str, discovery: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
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
