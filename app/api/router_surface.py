"""The router-composition surface shared by every identity-guarded router.
One owner of the caller-identity dependency run (fastapi + caller_auth.admit +
CallerDep/TrackingDep/get_settings) so a thin router declares only its own handler.
"""

from __future__ import annotations

from fastapi import Depends

from app.api.caller_auth import admit
from app.api.dependencies import (
    CallerDep,
    TrackingDep,
    get_settings,
    get_shutdown_event,
)

#: Pre-wired dependency values so a thin router need not name ``Depends(...)``.
#: A module-level ``Depends`` singleton is the idiomatic #322 router-plane shape.
settings_dep = Depends(get_settings)
shutdown_event_dep = Depends(get_shutdown_event)

__all__ = [
    "CallerDep",
    "TrackingDep",
    "admit",
    "settings_dep",
    "shutdown_event_dep",
]
