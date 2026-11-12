"""Lazily resolved reference to the running app module's accessor.

Keeps runtime/service callers off the fastapi graph crawlutron.main owns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

__all__ = ["AppModuleLike", "module_app", "set_module_app_provider"]


@runtime_checkable
class AppModuleLike(Protocol):
    """The slice of the ASGI app a sweep task reads: its ``state`` namespace."""

    @property
    def state(self) -> object: ...


_AppProvider = Callable[[], AppModuleLike | None]
_provider: _AppProvider | None = None


def set_module_app_provider(provider: _AppProvider) -> None:
    """Install (or replace) the app-module accessor; main wires this at import."""
    global _provider
    _provider = provider


def module_app() -> AppModuleLike | None:
    """The installed app-module accessor; main's ``module_app`` by default wiring."""
    if _provider is None:
        raise RuntimeError("module_app provider not installed: crawlutron.main not imported")
    return _provider()
