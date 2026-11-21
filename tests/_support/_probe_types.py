"""Type definitions for probe_session."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Protocol


class _HasCookies(Protocol):
    def cookies(self, target: str) -> Awaitable[list[dict[str, str]]]: ...
