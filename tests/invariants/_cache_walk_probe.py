"""Probe helper: a detector's second walk is served by the shared node cache."""

from __future__ import annotations

import ast
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
