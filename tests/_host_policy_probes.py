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
    """An empty recorder for the "this must never be reached" lists.

    The refusal tests all need the same thing: somewhere to record what the
    refused code would have done, so the refusal is proven by the list staying
    empty rather than by the absence of an exception. Eight sites spelled that out
    as a hand-built dict literal, each declaring its keys up front -- while the
    one assertion that consumes them never names a key and only walks whatever
    got recorded. So those key lists were ceremony: a test could declare
    ``{"webhook": []}``, record under ``"wire"``, and still pass, with the
    declared key never read by anything. Here a key exists only if something was
    recorded, so the recorder and the assertion cannot drift apart.
    """
    return defaultdict(list)


def _record_probe_fetch(effects: MutableMapping[str, list], payload: Mapping[str, Any]) -> None:
    """Note a fetch the guard was supposed to prevent, unless it was the blocked URL.

    The stand-in execution helpers all need the same recording: the only fetch
    that is *not* a finding is the one the guard was supposed to refuse. Spelled
    inline in each helper, a probe that compared a different value than the one
    the test asserted on would record every URL it saw -- and the assertion
    still passed, because the test only ever checked the blocked one.
    """
    if payload.get("url") != _BLOCKED_URL:
        effects["fetched"].append(payload.get("url"))


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
