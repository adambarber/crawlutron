"""Shared refusal probes: denied settings/policy, recorders and the refused-result check."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, MutableMapping

from safe_url.host_policy import HostPolicy

_BLOCKED_URL = "https://blocked.test/secret"


def _denied_settings():
    from crawlutron.config.settings import CrawlerSettings

    return CrawlerSettings.from_mapping({"CRAWLUTRON_DENIED_HOSTS": "blocked.test"})


def _denied_policy():
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


def _effects() -> defaultdict[str, list[str]]:
    """An empty recorder for the "this must never be reached" lists: refusal is proven
    by the list staying empty, and a key exists only once something was recorded, so
    the recorder and the consuming assertion cannot drift apart.
    """
    return defaultdict(list)


def monkeypatch_wire_guard(monkeypatch, effects) -> None:
    """Point ``httpx.AsyncClient`` at a recorder that never lets a blocked host reach the wire."""
    import httpx

    def forbidden_client(*args, **kwargs):
        effects["wire"].append(args)
        raise AssertionError("blocked webhook reached the wire")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)


def _record_probe_fetch(effects: MutableMapping[str, list[str]], payload: Mapping[str, object]) -> None:
    """Note a fetch the guard was supposed to prevent, except the blocked URL itself;
    spelled inline, a probe comparing a different value would record every URL and
    the assertion would still pass, since it only ever checked the blocked one.
    """
    url = getattr(payload, "url", None)
    if url is None:
        raw = payload.get("url")
        url = raw if isinstance(raw, str) else None
    if url is not None and url != _BLOCKED_URL:
        effects["fetched"].append(url)


async def _assert_refused(awaitable, effects: Mapping[str, list[str]]) -> None:
    try:
        result = await awaitable
    except (ValueError, RuntimeError):
        result = None
    except Exception as error:
        # Any refusal carrier refuses: the api HTTPException or a service DomainError subclass.
        if not (
            error.__class__.__name__ == "HTTPException"
            or "DomainError" in (c.__name__ for c in type(error).__mro__)
        ):
            raise
        result = None
    assert _refused_result(result)
    assert all(not values for values in effects.values())


def _blocked_records() -> list[dict[str, object]]:
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
