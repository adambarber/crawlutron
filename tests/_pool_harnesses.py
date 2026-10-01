"""Harnesses for the pooled-client seam: supplying a builder without patching.

The pooled httpx client and curl_cffi session in ``http_client_pool`` are
process-wide and built lazily on whichever event loop asks first, so nothing
stands at their construction site -- a caller cannot inject there, and neither
can a test. ``pooled_client_pool.pooled_client_builders`` is the argument that
supplies the builder anyway; the ``stub_builder`` fixture that wraps it lives in
``conftest.py``, which is where a fixture more than one module needs has to be
for pytest to discover it.

What is here is the other half: adapting a double to the builder contract. The
contract takes the settings a client would be configured from, because httpx
clients are, and a curl_cffi session is not. A fake factory for either usually
wants no arguments at all, so the adaptation is stated once here rather than as a
``lambda *a, **k:`` repeated in every test that supplies a double.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["builder_for"]


def builder_for(fake: Callable[[], Any]) -> Callable[..., Any]:
    """Adapt a zero-argument fake factory to the ``build(settings)`` contract."""

    def _build(settings: Any = None) -> Any:
        return fake()

    return _build
