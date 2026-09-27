import ast
import asyncio
import functools
import inspect
import json
import os
from contextlib import contextmanager

import pytest


def set_optional_env(monkeypatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def block_private_targets(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")


@contextmanager
def host_resolves_to(monkeypatch, ip: str):
    """The precondition for a resolution-dependent SSRF verdict: the name resolves to ``ip``.

    Opt out of the allow-private escape hatch *and* pin the resolver in one
    step, so a rejection under test is the policy's answer and not an accident
    of whatever the machine's resolver returned. ``ip="127.0.0.1"`` is the
    private case; a public address is the accept case, and both go through here
    so a test cannot assert one without the other half of the setup.

    Yields the list of hostnames resolved inside the block. Counting belongs
    here rather than in each test: "this name is resolved exactly once" is the
    other half of the DNS contract, and a fixed return value cannot observe it.
    """
    import socket
    from unittest.mock import patch

    block_private_targets(monkeypatch)
    resolved: list[str] = []

    def _getaddrinfo(host, *args, **kwargs):
        resolved.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    with patch("socket.getaddrinfo", side_effect=_getaddrinfo):
        yield resolved


def assert_dns_pinning_uses_first_resolution(monkeypatch, rebound_ip, public_ip="93.184.216.34") -> None:
    import socket
    from unittest.mock import patch

    from app.crawl.strategies.pinned_transport import _resolve_and_pin_host

    block_private_targets(monkeypatch)
    calls = []

    def fake_getaddrinfo(host, *a, **k):
        calls.append(host)
        if len(calls) == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (public_ip, 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (rebound_ip, 80))]

    with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
        _, ip = _resolve_and_pin_host("http://example.com/")
    assert ip == public_ip
    assert calls == ["example.com"]


def assert_result_success(result, classification: str = "rendered") -> None:
    assert result.status == "success"
    assert result.classification == classification


def _field(result, name: str):
    """Read ``name`` off a wire dict or a result object.

    The two spellings of the same assertion is the whole reason this exists: the
    endpoint tests read the serialized dict and the strategy tests read the
    result object, and the dict half had no shared assertion to call, so it
    restated two lines per test.
    """
    return result[name] if isinstance(result, dict) else getattr(result, name)


def assert_result_failed(result, classification: str) -> None:
    """A failed result whose status agrees with the classification table.

    ``status`` is read from the production table rather than restated as the
    literal ``"failed"``, so this cannot drift from what a classification
    actually means: a classification added to the table as skipped would fail
    here instead of being asserted into "failed" by a test that copied the
    string.
    """
    from app.constants import status_for_classification

    assert _field(result, "classification") == classification
    assert _field(result, "status") == status_for_classification(classification)


def assert_pool_unavailable(resp) -> None:
    """A 503 carrying the browser-pool-unavailable envelope.

    Two endpoint tests assert this shape and for two different reasons -- one
    that the guard fires before URL validation, one that an exhausted pool is
    reported rather than raised -- and they had the same three lines written
    out. Restated, a change to the envelope's code left one of them asserting a
    shape the server no longer returns, and that test still passed because the
    other half of the pair was the one being read.
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
        else:
            seen.append(getattr(fn, "__name__", repr(fn)))
        return await orig(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    return seen


def read_main_source() -> str:
    """The source of ``app.main``, imported inside the call.

    Two lifecycle suites need to assert on how main.py is written rather than on
    what it returns, and the import has to stay inside the function: ``app.main``
    is heavy, and the middleware suite has no other use for it. It was written
    out in both, so a change to what the read should see (a decorator, a
    re-export) was two edits and one of them would have been missed.
    """
    from app import main as main_mod

    return inspect.getsource(main_mod)


def extract_function_body(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                return segment
    raise AssertionError(f"Could not find function {name}")


def provider_results(*items) -> str:
    import json

    return json.dumps({"results": list(items)})


def one_search_result(override_settings, fake_provider_http, **overrides):
    """Pin the provider to Tavily and serve exactly one search result.

    The pairing is the fixture: a provider choice, a key, and a body in the
    provider's own wire shape. Two files were each writing it out, so a change to
    the provider envelope (or the key name) landed in one test and not the other.
    ``overrides`` tweaks a result field; pass ``items`` for a different count.
    """
    override_settings(search_provider="tavily", tavily_api_key="test-key")
    result = {"title": "A", "url": "https://example.gov/a", "content": "desc"}
    items = overrides.pop("items", None)
    if items is None:
        result.update(overrides)
        items = (result,)
    fake_provider_http(200, provider_results(*items))


@functools.lru_cache(maxsize=1)
def browser_available() -> bool:
    try:
        from camoufox.utils import launch_path

        path = launch_path()
        return bool(path) and os.path.exists(path)
    except Exception:
        pass
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


__all__ = [
    "assert_dns_pinning_uses_first_resolution",
    "assert_result_failed",
    "assert_result_success",
    "block_private_targets",
    "browser_available",
    "extract_function_body",
    "install_to_thread_spy",
    "poll_until",
    "poll_crawl_completion",
    "poll_webhook_delivery",
    "submit_async_crawl",
    "provider_results",
    "set_optional_env",
]


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


async def accepted_crawl(client, payload: dict) -> dict:
    """POST a crawl that must be accepted for background execution.

    Asserts the 202/``accepted`` contract once so a test that is about the
    webhook or the status URL is not also the place that re-spells the accept
    envelope; returns that envelope.
    """
    response = await client.post("/v1/crawl", json=payload)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    return body


async def completed_crawl(client, payload: dict) -> dict:
    """POST a crawl that must run synchronously and return its response body.

    The mirror of :func:`accepted_crawl`: 200 with the results inline. A test
    that wants a single result says ``(await completed_crawl(...))["results"][0]``
    rather than re-deriving which status code means "ran".
    """
    response = await client.post("/v1/crawl", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


async def submit_async_crawl(
    client,
    payload: dict,
    *,
    message: str = "Async crawl did not complete in time",
    on_pending=None,
):
    response = await client.post("/v1/crawl", json=payload)
    assert response.status_code == 202, response.text
    request_id = response.json()["request_id"]
    completed = await poll_crawl_completion(client, request_id, message=message, on_pending=on_pending)
    return request_id, completed
