"""Typed admission at the sidecar process boundary: one bind and policy door."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from safe_url import HostPolicy

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings

# Sidecar binds all interfaces (internal network) unless the operator names one.
# INADDR_ANY is the deployment default; the ingress relay narrows it further.
_ANY = ipaddress.IPv4Address(socket.INADDR_ANY)
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?\Z")
_EGRESS_PORT = 8888
_INGRESS_PORT = 8765

__all__ = ["SidecarConfig", "parse_port"]


def parse_port(raw: str | None, name: str, default: int) -> int:
    """Parse one fail-closed TCP port in 1..65535."""
    text = raw if raw is not None else str(default)
    if not (text.isascii() and text.isdecimal()):
        raise ValueError(f"{name} must be a decimal port, got {text!r}")
    value = int(text)
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be in 1..65535, got {value}")
    return value


def _bind_host(raw: str | None, name: str) -> ipaddress.IPv4Address:
    """Parse an optional IPv4 bind address, defaulting to the unspecified one."""
    if raw is None:
        return _ANY
    try:
        return ipaddress.IPv4Address(raw)
    except ValueError as exc:
        raise ValueError(f"bad {name} {raw!r}") from exc


@dataclass(frozen=True)
class SidecarConfig:
    """One admitted deployment config: bound endpoint, policy, fixed upstream."""

    bind_host: ipaddress.IPv4Address
    bind_port: int
    policy: HostPolicy | None = None
    upstream_host: str = ""
    upstream_port: int = 0

    @classmethod
    def for_egress(cls, env: Mapping[str, str], settings: CrawlerSettings) -> SidecarConfig:
        """Admit the egress bind and the policy derived from the shared settings."""
        # Outbound policy comes from the shared settings source, never the request.
        return cls(
            bind_host=_bind_host(env.get("EGRESS_PROXY_BIND_HOST"), "EGRESS_PROXY_BIND_HOST"),
            bind_port=parse_port(env.get("EGRESS_PROXY_BIND_PORT"), "EGRESS_PROXY_BIND_PORT", _EGRESS_PORT),
            policy=HostPolicy.from_settings(settings),
        )

    @classmethod
    def for_ingress(cls, env: Mapping[str, str]) -> SidecarConfig:
        """Admit the unspecified bind and a well-formed, fixed upstream."""
        bind_host = _bind_host(env.get("API_INGRESS_BIND_HOST"), "API_INGRESS_BIND_HOST")
        if bind_host != _ANY:
            raise ValueError("API_INGRESS_BIND_HOST must be 0.0.0.0")
        upstream_host = env.get("API_INGRESS_UPSTREAM_HOST", "crawlutron")
        if not (len(upstream_host) <= 253 and _HOST_RE.fullmatch(upstream_host)):
            raise ValueError(f"bad API_INGRESS_UPSTREAM_HOST {upstream_host!r}")
        return cls(
            bind_host=bind_host,
            bind_port=parse_port(env.get("API_INGRESS_BIND_PORT"), "API_INGRESS_BIND_PORT", _INGRESS_PORT),
            upstream_host=upstream_host,
            upstream_port=parse_port(
                env.get("API_INGRESS_UPSTREAM_PORT"), "API_INGRESS_UPSTREAM_PORT", _INGRESS_PORT
            ),
        )

    def egress_policy(self) -> HostPolicy:
        """Return the admitted outbound policy; only egress configs carry one."""
        if self.policy is None:
            raise ValueError("egress policy is not configured")
        return self.policy
