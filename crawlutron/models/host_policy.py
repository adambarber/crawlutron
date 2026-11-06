"""Which hosts a crawl may reach, independent of any one URL."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from crawlutron.config.env import EnvDecisionMemo
from crawlutron.models.address_class import AddressClass, classify
from crawlutron.runtime import process_state


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


def host_policy_reason(host: str, *, policy: HostPolicy) -> str | None:
    """Why this host is refused, or None when the policy admits it. Hosts here are
    admitted values (punycode via admission) or already-normalized config entries."""
    if any(host == entry or host.endswith(f".{entry}") for entry in policy.denied_hosts):
        return "host_denied"
    if policy.allowed_hosts and not any(
        host == entry or host.endswith(f".{entry}") for entry in policy.allowed_hosts
    ):
        return "host_not_allowed"
    return None


#: Hosts that name this machine rather than a site on the internet.
_PRIVATE_HOSTS = frozenset({"localhost", "host.docker.internal"})

#: (raw env value, parsed decision) for CRAWLUTRON_ALLOW_PRIVATE_TARGETS.
#: Value-keyed: an env change lands on the next call, an unchanged one skips it.
_PRIVATE_TARGET_DECISION = EnvDecisionMemo(
    "CRAWLUTRON_ALLOW_PRIVATE_TARGETS",
    default=False,
    logger_name="crawlutron.models.host_policy",
    warning=(
        "SSRF private-target guard DISABLED by CRAWLUTRON_ALLOW_PRIVATE_TARGETS=%r: "
        "caller-supplied URLs may now reach private, loopback, and link-local "
        "addresses, including cloud metadata endpoints. Unset it, or set it to 0, "
        "outside local development."
    ),
)


def allow_private_targets() -> bool:
    """Memoized read of the private-target escape hatch (inverted default).

    Host verdicts are a pure function of the host and never go stale; the env
    decision memo warns once per distinct value, not per call.
    """
    return _PRIVATE_TARGET_DECISION.decide()


def reset_private_target_memo() -> None:
    """Drop the memoized env decision (tests that count parse calls only).

    Host verdicts are a pure function of the host and never go stale, so the
    verdict memo deliberately is not touched here.
    """
    _PRIVATE_TARGET_DECISION.reset()


def _reset() -> None:
    """Drop the memoized env decision (registry route, conftest)."""
    reset_private_target_memo()


def _has() -> bool:
    """Whether an env decision is memoized right now."""
    return _PRIVATE_TARGET_DECISION._decision is not None


process_state.register(__name__, _reset, _has)


@lru_cache(maxsize=8192)
def _host_is_public(host: str) -> bool:
    """SSRF verdict for one canonical host (bounded memo, pure function).

    Deterministic per host and guarded-call-independent, so entries never go stale;
    the bounded memo caps attacker-influenced host floods from growing memory.
    """
    if host in _PRIVATE_HOSTS:
        return False
    if "%" in host:
        # A zone id is never a routable target: Python 3.12 ipaddress parses scoped
        # literals as PUBLIC, so refusal must be unconditional.
        return False
    # A host that is not an address is a name, judged against ``is_public_ip_value``
    # once the resolver answers; only a literal IP in a rejected range is refused.
    return classify(host) in (AddressClass.NOT_AN_ADDRESS, AddressClass.PUBLIC)


def host_egress_reason(host: str) -> str | None:
    """Why this host may not be reached, or None when the SSRF guard admits it.

    Separate from ``host_policy_reason``: that asks "on the list?", this "can we leave?".
    The list check runs first, so an unlisted-but-private host answers ``host_not_allowed``.
    """
    if allow_private_targets() or _host_is_public(host):
        return None
    return "private_network_target"


__all__ = [
    "HostPolicy",
    "allow_private_targets",
    "host_egress_reason",
    "host_policy_reason",
    "reset_private_target_memo",
]
