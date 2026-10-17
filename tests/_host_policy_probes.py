"""Shared refusal probes: denied settings/policy, recorders and the refused-result check."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, MutableMapping
from typing import Any

_BLOCKED_URL = "https://blocked.test/secret"


def _denied_settings():
    from app.config.settings import CrawlerSettings

    return CrawlerSettings.from_mapping({"CRAWLUTRON_DENIED_HOSTS": "blocked.test"})


def _denied_policy():
    from app.safety.validation import HostPolicy

    return HostPolicy.from_strings("", "blocked.test")


def _refused_result(result) -> bool:
    if getattr(result, "is_error", False):
        return True
    if isinstance(result, dict):
        results = result.get("results")
        if isinstance(results, list):
            return all(
                isinstance(item, dict) and item.get("status") in {"blocked", "disallowed", "failed"}
                for item in results
            )
        return not result.get("url") and not result.get("links")
    return result is None


def _effects() -> defaultdict[str, list[Any]]:
    """An empty recorder for the "this must never be reached" lists: refusal is proven
    by the list staying empty, and a key exists only once something was recorded, so
    the recorder and the consuming assertion cannot drift apart.
    """
    return defaultdict(list)


def _record_probe_fetch(effects: MutableMapping[str, list], payload: Mapping[str, Any]) -> None:
    """Note a fetch the guard was supposed to prevent, except the blocked URL itself;
    spelled inline, a probe comparing a different value would record every URL and
    the assertion would still pass, since it only ever checked the blocked one.
    """
    url = getattr(payload, "url", None) or payload.get("url")
    if url != _BLOCKED_URL:
        effects["fetched"].append(url)


async def _assert_refused(awaitable, effects: Mapping[str, list]) -> None:
    try:
        result = await awaitable
    except (ValueError, RuntimeError):
        result = None
    except Exception as error:
        if error.__class__.__name__ != "HTTPException":
            raise
        result = None
    assert _refused_result(result)
    assert all(not values for values in effects.values())


def _blocked_records() -> list[dict]:
    """One retained crawl record whose only page was blocked."""
    return [
        {
            "request_id": "req-1",
            "results": [
                {
                    "normalized_url": _BLOCKED_URL,
                    "status": "blocked",
                    "classification": "captcha_detected",
                    "selected_strategy": None,
                    "attempted_strategies": [],
                    "classification_evidence": [],
                }
            ],
        }
    ]
