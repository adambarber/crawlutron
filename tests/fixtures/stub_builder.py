"""``stub_builder``: supply a pooled-client builder through the public seam, where
nothing stands at a process-wide lazy construction site. The fixture guarantees
restore (a leaked builder fails only on a rerun) and one-kind-at-a-time.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Generator
from typing import Any

import pytest

from crawlutron.transport import http_client_pool

__all__ = ["stub_builder"]


@pytest.fixture
def stub_builder() -> Generator[Callable[[str, Callable[..., Any]], None], None, None]:
    """Install a pooled-client builder for this test's body, undone at teardown.

    ``seam`` is ``"http"`` or ``"cffi"`` -- the keyword the public seam accepts, not
    a private function name; ``builder_for`` adapts a zero-argument double to it.
    """
    entered: list[contextlib.ExitStack] = []

    def _install(seam: str, builder: Callable[..., Any]) -> None:
        stack = contextlib.ExitStack()
        stack.enter_context(http_client_pool.pooled_client_builders(**{seam: builder}))
        entered.append(stack)

    yield _install
    for stack in reversed(entered):
        stack.close()
