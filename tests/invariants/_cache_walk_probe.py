"""Probe helpers: a memoized walk's repeat call is served from the cache."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable
from unittest import mock


def assert_second_call_skips_the_walk(call: Callable[..., object], *args: object, root: ast.AST) -> object:
    """Walk ``call(*args)`` on ``root`` twice; the second call must not re-walk it.

    Returns the second result so the caller can pin determinism."""
    walked: list[ast.AST] = []
    real_walk = ast.walk

    def counting(node: ast.AST):
        walked.append(node)
        return real_walk(node)

    with mock.patch("ast.walk", counting):
        result = call(*args)
    assert root not in walked, "detector re-walked the tree; node cache not reused"
    return result


def assert_repeat_call_skips_the_walk[T](call: Callable[[], T]) -> T:
    """Call ``call()`` twice against a cold cache; the repeat must reuse the first result.

    Identity is the order-independent clause: a warmed cache still hands back one object."""
    original = pathlib.Path.rglob
    with mock.patch.object(pathlib.Path, "rglob", autospec=True, side_effect=original) as spy:
        first = call()
        walked_once = spy.call_count
        second = call()
    assert walked_once > 0, "the first measured call walked nothing; the derivation is not filesystem-derived"
    assert second is first, "a repeat call built a second object; the declaration carries no cache"
    extra_walks = spy.call_count - walked_once
    assert not extra_walks, f"a repeat call walked the tree {extra_walks} more times"
    return second
