"""The two pre-wired DI singletons shared by every thin router.

A module-level ``Depends`` singleton is the idiomatic router-plane shape; caller
identity lives in ``crawlutron_rest.api.caller_auth`` and ``crawlutron_rest.api.dependencies``.
"""

from __future__ import annotations

from fastapi import Depends

from crawlutron_rest.api.dependencies import get_settings, get_shutdown_event

#: Pre-wired dependency values so a thin router need not name ``Depends(...)``.
settings_dep = Depends(get_settings)
shutdown_event_dep = Depends(get_shutdown_event)

__all__ = ["settings_dep", "shutdown_event_dep"]
