"""Which hosts a crawl may reach, independent of any one URL."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from app.config.env import parse_bool_env
from app.models.address_class import AddressClass, classify


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
    "app/api/job_runner.py:run_job",
    "app/api/jobs_router.py:create_job",
    "app/api/map_endpoint.py:map_site",
    "app/api/render.py:render_request",
    "app/api/async_crawl_runner.py:run_async_crawl",
    "app/api/response_shaping.py:frontier_link_url",
    "app/api/watch_router.py:create_watch",
    "app/api/policy_suggestions.py:policy_suggestions",
    "app/api/site_map.py:build_site_map",
    "app/api/policy_suggestions.py:build_policy_suggestions",
    "app/api/webhooks.py:post_webhook",
    "app/api/webhooks.py:deliver_pinned_webhook",
    "app/mcp/tools.py:handle_crawl",
    "app/mcp/tools.py:handle_crawl_and_extract",
    "app/mcp/tools.py:handle_map",
    "app/mcp/tools.py:handle_render",
    "app/mcp/tools.py:handle_search",
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


#: Hosts that name this machine rather than a site on the internet.
_PRIVATE_HOSTS = frozenset({"localhost", "host.docker.internal"})

#: (raw env value, parsed decision) for CRAWLUTRON_ALLOW_PRIVATE_TARGETS.
#: Value-keyed: an env change lands on the next call, an unchanged one skips it.
_PRIVATE_TARGET_DECISION: tuple[str | None, bool] | None = None


def allow_private_targets() -> bool:
    """Memoized read of the private-target escape hatch (inverted default).

    The raw env value is the cache key: unrecognized values keep resolving to
    ``False`` (guard ON) via ``parse_bool_env``, and every distinct change honors immediately.
    """
    global _PRIVATE_TARGET_DECISION
    raw = os.environ.get("CRAWLUTRON_ALLOW_PRIVATE_TARGETS")
    cached = _PRIVATE_TARGET_DECISION
    if cached is not None and cached[0] == raw:
        return cached[1]
    decision = parse_bool_env("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", default=False)
    _PRIVATE_TARGET_DECISION = (raw, decision)
    if decision:
        # Warned at the decision, not per call: this is the per-fetch SSRF path,
        # so the memo logs once per distinct env value instead of flooding.
        logging.getLogger(__name__).warning(
            "SSRF private-target guard DISABLED by CRAWLUTRON_ALLOW_PRIVATE_TARGETS=%r: "
            "caller-supplied URLs may now reach private, loopback, and link-local "
            "addresses, including cloud metadata endpoints. Unset it, or set it to 0, "
            "outside local development.",
            raw,
            extra={"env_name": "CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "env_value": raw},
        )
    return decision


def reset_private_target_memo() -> None:
    """Drop the memoized env decision (tests that count parse calls only).

    Host verdicts are a pure function of the host and never go stale, so the
    verdict memo deliberately is not touched here.
    """
    global _PRIVATE_TARGET_DECISION
    _PRIVATE_TARGET_DECISION = None


@lru_cache(maxsize=8192)
def _host_is_public(host: str) -> bool:
    """SSRF verdict for one canonical host (bounded memo, pure function).

    Deterministic per host and guarded-call-independent, so entries never go stale;
    the bounded memo caps attacker-influenced host floods from growing memory.
    """
    return classify(host) in (AddressClass.NOT_AN_ADDRESS, AddressClass.PUBLIC)


def host_egress_reason(host: str) -> str | None:
    """Why this host may not be reached, or None when the SSRF guard admits it.

    Separate from ``host_policy_reason``: that asks "on the list?", this "can we leave?".
    The list check runs first, so an unlisted-but-private host answers ``host_not_allowed``.
    """


__all__ = [
    "HOST_POLICY_ENTRY_POINTS",
    "HostPolicy",
    "allow_private_targets",
    "host_egress_reason",
    "host_policy_reason",
    "reset_private_target_memo",
]
