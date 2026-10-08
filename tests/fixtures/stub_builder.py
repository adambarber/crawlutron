"""``stub_builder``: supply a pooled-client builder through the public seam.

The pooled httpx client and curl_cffi session in ``http_client_pool`` are
process-wide and built lazily on whichever event loop asks first. Nothing stands
at their construction site, so a caller cannot inject there -- which is exactly
why the builders used to be monkeypatched on the module, and why the module's own
docstring advertised a late-bound lambda as a design goal so the patch would be
observed. ``pooled_client_builders`` replaces that with an argument; this is the
test-side half of it.

Two things this fixture exists to guarantee, neither of which a per-test helper
can:

**Restore.** The pool outlives the block and the process, so a builder left
installed is inherited by every later test in the worker that asks for a pooled
client. That presents as a flaky assertion and is not one -- it depends on
execution order, which is why it survives a green run and fails on a rerun.

**One kind at a time.** Supplying ``cffi`` leaves the httpx pool's builder alone.
A helper that installed both would silently rebuild the httpx client's identity
as a side effect of a test that only meant to touch curl_cffi.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import pytest

from app.transport import http_client_pool

__all__ = ["stub_builder"]


@pytest.fixture
def stub_builder() -> Callable[[str, Callable[..., Any]], None]:
    """Install a pooled-client builder for this test's body, undone at teardown.

    ``seam`` is ``"http"`` or ``"cffi"`` -- the keyword the public seam accepts,
    not the name of a private module function. The builder is what the pool calls
    to produce a client; ``tests._pool_harnesses.builder_for`` adapts a
    zero-argument double to that contract.
    """
    entered: list[contextlib.ExitStack] = []

    def _install(seam: str, builder: Callable[..., Any]) -> None:
        stack = contextlib.ExitStack()
        stack.enter_context(http_client_pool.pooled_client_builders(**{seam: builder}))
        entered.append(stack)

    yield _install
    for stack in reversed(entered):
        stack.close()
