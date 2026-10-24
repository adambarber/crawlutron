"""Adapt a zero-argument fake factory to the ``build(settings)`` builder contract.

The pooled httpx client and curl_cffi session are process-wide and lazily built,
so this adapter states once instead of a ``lambda *a, **k`` how a no-arg double fits.
"""

from __future__ import annotations

from collections.abc import Callable

__all__ = ["builder_for"]


def builder_for(fake: Callable[[], object]) -> Callable[..., object]:
    """Adapt a zero-argument fake factory to the ``build(settings)`` contract."""

    def _build(settings: object = None) -> object:
        return fake()

    return _build
