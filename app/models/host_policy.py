"""Which hosts a crawl may reach, independent of any one URL."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse


def _normalize_policy_host(host: str) -> str:
    value = host.strip().lower().rstrip(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return value


def _policy_host_entry(entry: str) -> str:
    value = entry.strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = parsed.hostname or value.split("/", 1)[0].split(":", 1)[0]
    if host.startswith("*."):
        host = host[2:]
    return _normalize_policy_host(host)


@lru_cache(maxsize=256)
def _policy_hosts(raw: str) -> tuple[str, ...]:
    return tuple(entry for item in raw.split(",") if (entry := _policy_host_entry(item)))


@dataclass(frozen=True)
class HostPolicy:
    allowed_hosts: tuple[str, ...]
    denied_hosts: tuple[str, ...]

    @classmethod
    def unrestricted(cls) -> HostPolicy:
        return cls((), ())

    @classmethod
    def from_strings(cls, allowed_hosts: str, denied_hosts: str) -> HostPolicy:
        return cls(_policy_hosts(allowed_hosts), _policy_hosts(denied_hosts))

    @classmethod
    def from_settings(cls, settings: object) -> HostPolicy:
        return cls.from_strings(
            str(getattr(settings, "allowed_hosts", "") or ""),
            str(getattr(settings, "denied_hosts", "") or ""),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_hosts",
            tuple(_policy_host_entry(entry) for entry in self.allowed_hosts if _policy_host_entry(entry)),
        )
        object.__setattr__(
            self,
            "denied_hosts",
            tuple(_policy_host_entry(entry) for entry in self.denied_hosts if _policy_host_entry(entry)),
        )


HOST_POLICY_ENTRY_POINTS = (
    "app/api/crawl_endpoint.py:crawl",
    "app/api/crawl_executor.py:execute_crawl",
    "app/api/jobs_router.py:create_job",
    "app/api/map_endpoint.py:map_site",
    "app/api/render.py:render_request",
    "app/api/async_crawl_runner.py:run_async_crawl",
    "app/api/response_shaping.py:frontier_link_url",
    "app/api/webhooks.py:post_webhook",
    "app/api/webhooks.py:deliver_pinned_webhook",
    "app/mcp/tools.py:_handle_crawl",
    "app/mcp/tools.py:_handle_map",
    "app/mcp/tools.py:_handle_render",
    "app/mcp/tools.py:_handle_search",
)


def host_policy_reason(host: str, *, policy: HostPolicy) -> str | None:
    """Why this host is refused, or None when the policy admits it."""
    normalized = _normalize_policy_host(host)
    if any(normalized == entry or normalized.endswith(f".{entry}") for entry in policy.denied_hosts):
        return "host_denied"
    if policy.allowed_hosts and not any(
        normalized == entry or normalized.endswith(f".{entry}") for entry in policy.allowed_hosts
    ):
        return "host_not_allowed"
    return None


__all__ = ["HOST_POLICY_ENTRY_POINTS", "HostPolicy", "host_policy_reason"]
