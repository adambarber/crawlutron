"""Lazily resolved reference to the running app module's accessor.

Keeps runtime/service callers off the fastapi graph crawlutron_rest.main owns.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Protocol, runtime_checkable

__all__ = [
    "AppModuleLike",
    "module_app",
    "provider_wiring_snapshot",
    "reset_module_app_provider",
    "restore_provider_wiring",
    "set_module_app_provider",
]


@runtime_checkable
class AppModuleLike(Protocol):
    """The slice of the ASGI app a sweep task reads: its ``state`` namespace."""

    @property
    def state(self) -> object: ...


_AppProvider = Callable[[], AppModuleLike | None]
_provider: _AppProvider | None = None
_install_site: str | None = None


def set_module_app_provider(provider: _AppProvider) -> None:
    """Install the app-module accessor; exactly once, main wires this at import."""
    global _provider, _install_site
    caller = traceback.extract_stack()[-2]
    site = f"{caller.filename}:{caller.lineno}"
    if _provider is not None:
        if site == _install_site:
            return  # same call site re-executed (module re-import); not a second site
        raise RuntimeError(
            f"module_app provider already installed at {_install_site}; "
            f"refusing re-install from {site} (reset_module_app_provider is the door)"
        )
    _provider = provider
    _install_site = site


def reset_module_app_provider() -> None:
    """The sanctioned reinstall door: uninstall, refusing loudly when nothing is installed."""
    global _provider, _install_site
    if _provider is None:
        raise RuntimeError("module_app provider not installed: nothing to reset")
    _provider = None
    _install_site = None


def module_app() -> AppModuleLike | None:
    """The installed app-module accessor; main's ``module_app`` by default wiring."""
    if _provider is None:
        raise RuntimeError("module_app provider not installed: crawlutron_rest.main not imported")
    return _provider()


def provider_wiring_snapshot() -> tuple[_AppProvider | None, str | None] | None:
    """The installed (provider, site) pair for state-reset fixtures; None when uninstalled."""
    if _provider is None:
        return None
    return (_provider, _install_site)


def restore_provider_wiring(snapshot: tuple[_AppProvider | None, str | None] | None) -> None:
    """Restore a :func:`provider_wiring_snapshot` value; the state-reset fixture door."""
    global _provider, _install_site
    if snapshot is None:
        _provider, _install_site = None, None
        return
    _provider, _install_site = snapshot
