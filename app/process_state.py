"""The contract a module accepts when it holds state that outlives one call.

A module that caches per-host DNS answers, a cookie jar, a shared HTTP client, a
sitemap document or a resource gate holds something the *next* caller inherits.
Production wants that -- a host crawled thousands of times must not pay a blocking
``getaddrinfo`` per fetch -- and a test does not, because a stale entry from one
test is indistinguishable from a real cache hit in the next.

So the obligation falls on the owner: whoever holds the state publishes the reset.
The alternative is a caller reaching in to ``.clear()`` it, which is how this
started -- seven hand-rolled clears across four test files, and sixteen reaches
into five modules from the autouse fixture alone.

Shape is only half the contract, which is why this protocol has two members rather
than one. ``reset_process_state`` says the owner can be emptied; it says nothing
about whether emptying it *worked*, and a double that implements the method with a
``pass`` satisfies a one-member protocol perfectly. ``has_process_state`` is the
observable half, and it is the half a conformance test can actually check: reset,
then ask. A protocol whose only test is ``isinstance`` teaches a reader that
duck typing is about method names, which is the misconception this module exists
to close.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProcessStateOwner(Protocol):
    """A module holding state that a later caller would otherwise inherit."""

    def reset_process_state(self) -> None:
        """Drop everything this module holds that outlives a single call."""
        ...

    def has_process_state(self) -> bool:
        """Whether anything is currently held.

        Honest in both directions. A module reporting ``False`` while holding a
        populated cache is worse than having no probe at all, because the
        conformance test then certifies a reset that never emptied anything.
        """
        ...
