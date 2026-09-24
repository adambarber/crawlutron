from typing import Any

from app.constants import HTTP_DECLARED_CRAWLER
from app.validation import HostPolicy

UNRESTRICTED_HOST_POLICY = HostPolicy.unrestricted()

BOOL_ENV_CASES = (
    (None, True),
    ("", True),
    ("   ", True),
    ("1", True),
    (" TrUe ", True),
    ("yes", True),
    ("ON", True),
    ("0", False),
    (" FALSE ", False),
    ("No", False),
    ("off", False),
    ("garbage", True),
)


def make_base_policy(strategies=None) -> dict:
    return {
        "schema_version": "2026-06-23",
        "strategies": list(strategies) if strategies is not None else [HTTP_DECLARED_CRAWLER],
        "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
        "headers": {},
        "request_profile": {},
        "browser_profile": {},
        "discovery": {},
    }


def _policy(extra=None):
    policy = {
        "request_profile": {},
        "headers": {},
        "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
    }
    if extra:
        policy.update(extra)
    return policy


def http_strategy_policy(*, extraction: str = "full", discovery: bool | None = None) -> dict:
    policy: dict[str, Any] = {
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "extraction": extraction,
        "headers": {},
        "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
    }
    if discovery is not None:
        policy["discovery"] = {"links": True} if discovery else {}
    return policy


__all__ = [
    "BOOL_ENV_CASES",
    "_policy",
    "UNRESTRICTED_HOST_POLICY",
    "http_strategy_policy",
    "make_base_policy",
]
