import asyncio
import contextlib
import functools
import inspect
import json
import logging
import os
from contextlib import contextmanager

import pytest

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.json_value import JsonValue
from tests._policy_factories import UNRESTRICTED_HOST_POLICY


def set_optional_env(monkeypatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def block_private_targets(monkeypatch) -> None:
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")


@contextmanager
def plaintext_webhook_refusal(monkeypatch):
    """The closed #499 door for one call: drop the plaintext opt-in and its memo."""
    monkeypatch.delenv("CRAWLUTRON_ALLOW_PLAINTEXT_WEBHOOK", raising=False)
    from crawlutron.api.webhooks import reset_plaintext_webhook_memo

    reset_plaintext_webhook_memo()
    try:
        yield
    finally:
        reset_plaintext_webhook_memo()


@contextmanager
def host_resolves_to(monkeypatch, ip: str, *, resolver=None):
    """The precondition for a resolution-dependent SSRF verdict: ``name`` resolves to ``ip``.

    ``resolver`` replaces the whole ``getaddrinfo`` (a mixed-answer pin); a plain
    ``ip`` pins every answer to that single address.
    """
    import socket
    from unittest.mock import patch

    block_private_targets(monkeypatch)
    resolved: list[str] = []

    def _getaddrinfo(host, *args, **kwargs):
        resolved.append(host)
        if resolver is not None:
            return resolver(host, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    with patch("socket.getaddrinfo", side_effect=_getaddrinfo):
        yield resolved


def resolves_in_sequence(monkeypatch, *ips: str) -> list[str]:
    """A fresh-resolve stub answering with ``ips`` in order (the last repeats),
    recording every host it was asked for.

    The browser stability check (#540) is a verdict over CONSECUTIVE fresh
    answers, so the stub hands out a scripted sequence rather than one pinned
    IP: answer n goes to lookup n.
    """
    import socket

    block_private_targets(monkeypatch)
    asked: list[str] = []

    def _getaddrinfo(host, *args, **kwargs):
        asked.append(host)
        ip = ips[min(len(asked) - 1, len(ips) - 1)]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)
    return asked


def assert_dns_pinning_uses_first_resolution(monkeypatch, rebound_ip, public_ip="93.184.216.34") -> None:
    import socket
    from unittest.mock import patch

    from crawlutron.transport.pinned_dns import resolve_and_pin_host

    block_private_targets(monkeypatch)
    calls = []

    def fake_getaddrinfo(host, *a, **k):
        calls.append(host)
        if len(calls) == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (public_ip, 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (rebound_ip, 80))]

    from tests._policy_factories import canonical

    with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
        _, ip = resolve_and_pin_host(canonical("http://example.com/"))
    assert ip == public_ip
    assert calls == ["example.com"]


def assert_result_success(result, classification: str = "rendered") -> None:
    assert result.status == "success"
    assert result.classification == classification


def load_spill_checked(caplog, spill):
    """The #C8 reassembly read under the engine logger's WARNING capture."""
    with caplog.at_level(logging.WARNING, logger="crawlutron.orchestration.engine"):
        return spill.load_ordered(policy=UNRESTRICTED_HOST_POLICY)


def assert_skipped_spill_line(caplog, *, served: list[EngineCrawlResult], expected: list[str]) -> None:
    """The #C8 contract in one assertion: the good results still serve and the
    corrupt line is logged, not silent -- the two spellings restated it."""
    assert [r.original_url for r in served] == expected, f"results lost: {served}"
    assert any("skipping corrupt spill line" in m for m in caplog.messages), caplog.messages


def _field(result, name: str):
    """Read ``name`` off a wire dict or a result object.

    The endpoint tests read the serialized dict, the strategy tests the result
    object -- one shared assertion instead of two restated spellings.
    """
    return result[name] if isinstance(result, dict) else getattr(result, name)


def wire_results(body: dict[str, JsonValue]) -> list[JsonValue]:
    """The ``results`` list of a crawl envelope, checked to be a list.

    Every endpoint test indexes the first result; narrowing here keeps the
    wire's JsonValue union from reaching each call site."""
    results = body["results"]
    assert isinstance(results, list), body
    return results


def wire_result(body: dict[str, JsonValue], index: int = 0) -> dict[str, JsonValue]:
    """One result of a crawl envelope, checked to be a dict."""
    result = wire_results(body)[index]
    assert isinstance(result, dict), result
    return result


def wire_dict(value: JsonValue, key: str) -> dict[str, JsonValue]:
    """A dict-valued member of a wire payload, checked so indexing stays honest."""
    assert isinstance(value, dict), (key, value)
    return value


def wire_member(result: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    """A result's named sub-dict, checked to be present and a dict.

    Unlike :func:`wire_dict` this names a missing key as such, so a dropped
    diagnostics block fails as an absent contract, not a wrong type."""
    member = result.get(key)
    assert isinstance(member, dict), f"result[{key!r}] missing or not a dict: {result}"
    return member


def assert_result_failed(result, classification: str) -> None:
    """A failed result whose status agrees with the classification table.

    ``status`` is read from production, not restated as ``"failed"``, so a
    classification added as skipped cannot be asserted into "failed".
    """
    from crawlutron.vocabulary.taxonomy import status_for_classification

    assert _field(result, "classification") == classification
    assert _field(result, "status") == status_for_classification(classification)


def assert_pool_unavailable(resp) -> None:
    """A 503 carrying the browser-pool-unavailable envelope.

    Two endpoint tests asserted this shape for two reasons with the same lines
    written out; when the envelope changed, one kept passing on a stale shape.
    """
    assert resp.status_code == 503, resp.status_code
    assert resp.json()["code"] == "browser_pool_unavailable", resp.json()


async def poll_until(predicate, *, timeout: float = 10.0, interval: float = 0.1, message: str | None = None):
    async def _runner() -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            value = predicate()
            if inspect.isawaitable(value):
                value = await value
            if value:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(interval)

    try:
        ok = await asyncio.wait_for(_runner(), timeout=timeout + 1.0)
    except TimeoutError:
        ok = False
    if not ok:
        pytest.fail(message or f"poll_until timed out after {timeout}s")
    return True


def install_to_thread_spy(monkeypatch):
    seen: list[str] = []
    orig = asyncio.to_thread

    async def spy(fn, *args, **kwargs):
        if isinstance(fn, functools.partial):
            seen.append(getattr(fn.func, "__name__", repr(fn.func)))
        elif getattr(fn, "__name__", "") == "<lambda>" and fn.__closure__:
            # A lambda offload wraps the real callee; its name rides the closure.
            seen.extend(
                getattr(cell.cell_contents, "__name__", str(cell.cell_contents)) for cell in fn.__closure__
            )
        else:
            seen.append(getattr(fn, "__name__", repr(fn)))
        return await orig(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    return seen


def provider_results(*items) -> str:
    import json

    return json.dumps({"results": list(items)})


def one_search_result(override_settings, fake_provider_http, **overrides):
    """Pin the provider to Tavily and serve exactly one search result.

    The pairing IS the fixture (provider, key, wire-shaped body); ``overrides``
    tweaks a result field, ``items`` changes the count.
    """
    override_settings(search_provider="tavily", tavily_api_key="test-key")
    result = {"title": "A", "url": "https://example.gov/a", "content": "desc"}
    items = overrides.pop("items", None)
    if items is None:
        result.update(overrides)
        items = (result,)
    fake_provider_http(200, provider_results(*items))


def awaitable_search(provider, query: str, limit: int):
    """Run one provider query to completion from a synchronous test.

    The provider's contract is async; a test that wants its results runs them
    here rather than each test module growing its own copy of this.
    """
    import asyncio

    return asyncio.run(provider.search(query, limit, timeout_seconds=1.0))


@functools.lru_cache(maxsize=1)
def browser_available() -> bool:
    # Both probes import inside the function, matching the idiom the playwright probe below already used.
    with contextlib.suppress(Exception):
        # A local predicate, never a network action: `launch_path()` auto-installs
        # (GitHub API) when the paired build is absent; the offline probe just raises.
        from camoufox.pkgman import LAUNCH_FILE, OS_NAME, camoufox_path

        directory = camoufox_path(download_if_missing=False)
        executable = (
            directory / "Camoufox.app" / "Contents" / "MacOS" / "camoufox"
            if OS_NAME == "mac"
            else directory / LAUNCH_FILE[OS_NAME]
        )
        return executable.exists()
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
            return bool(path) and os.path.exists(path)
    except Exception:
        return False


async def poll_webhook_delivery(records, *, timeout: float = 10.0, interval: float = 0.1):
    await poll_until(
        lambda: len(records) == 1,
        timeout=timeout,
        interval=interval,
        message="Webhook not delivered in time",
    )
    return json.loads(records[0].decode("utf-8"))


async def poll_crawl_completion(
    client,
    request_id: str,
    *,
    timeout: float = 15.0,
    interval: float = 0.1,
    message: str = "Async crawl did not complete in time",
    on_pending=None,
):
    response = None

    async def done() -> bool:
        nonlocal response
        response = await client.get(f"/v1/crawl/{request_id}")
        if response.status_code != 200 and on_pending is not None:
            on_pending(response)
        return response.status_code == 200

    await poll_until(done, timeout=timeout, interval=interval, message=message)
    return response


async def accepted_crawl(client, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """POST a crawl that must be accepted for background execution.

    Asserts the 202/``accepted`` contract once, so a webhook or status-URL test
    is not also the place that re-spells the accept envelope; returns it.
    """
    response = await client.post("/v1/crawl", json=payload)
    assert response.status_code == 202, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert body["status"] == "accepted"
    return body


async def completed_crawl(client, payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """POST a crawl that must run synchronously and return its response body.

    The mirror of :func:`accepted_crawl`: 200 with results inline, so a
    single-result test reads ``["results"][0]``, not re-derived codes.
    """
    response = await client.post("/v1/crawl", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


async def submit_async_crawl(
    client,
    payload: dict[str, JsonValue],
    *,
    message: str = "Async crawl did not complete in time",
    on_pending=None,
):
    response = await client.post("/v1/crawl", json=payload)
    assert response.status_code == 202, response.text
    request_id = response.json()["request_id"]
    completed = await poll_crawl_completion(client, request_id, message=message, on_pending=on_pending)
    return request_id, completed
