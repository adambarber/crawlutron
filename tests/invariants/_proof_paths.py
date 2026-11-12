"""Shared proof-site paths and tree scopes for the ownership invariant modules.

Declared support surface (#476): a sibling test module imports these from
here, not from another test module's internals.
"""

from __future__ import annotations

from tests.chokepoint_walks import APP_ROOT

__all__ = ["ELSEWHERE"]

#: A sanctioned `at=` path outside a rule's owner module: the "proves quiet
#: elsewhere" half of a path-keyed bite.
ELSEWHERE = APP_ROOT / "crawl" / "content_cache.py"
