"""The ``diagnostics["cooldown"]`` block: one domain cooldown, read by the chain."""

from typing import TypedDict


class CooldownDiagnostics(TypedDict):
    """The ``diagnostics["cooldown"]`` block: one domain cooldown, read by the chain."""

    retry_after_seconds: int | float
    scope: str
