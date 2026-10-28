"""One example policy template: strategies, optional extraction, and limits."""

from typing import TypedDict


class ExamplePolicy(TypedDict, total=False):
    """One example policy template the hints endpoint hands agents."""

    strategies: list[str]
    extraction: str
    limits: dict[str, int]
