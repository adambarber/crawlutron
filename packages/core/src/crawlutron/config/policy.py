"""Runtime policy snapshot handed to lower layers so they never import
crawlutron.config.settings directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimePolicy:
    """Read-only snapshot of the settings lower layers actually need.

    Secret fields carry repr=False so diagnostics never echo a live key.
    """

    search_provider: str
    tavily_api_key: str = field(repr=False)
    brave_api_key: str = field(repr=False)
    serper_api_key: str = field(repr=False)
    key_admin_token: str = field(repr=False)
    search_limit_default: int
    search_limit_max: int
    search_timeout_seconds: int
    respect_robots: bool
    allowed_hosts: str
    denied_hosts: str
    default_timeout_seconds: int
    default_max_retries: int
