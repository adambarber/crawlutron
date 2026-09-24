import ast
import asyncio
import functools
import inspect
import os

import pytest


def set_optional_env(monkeypatch, name: str, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)


def block_private_targets(monkeypatch) -> None:
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")


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


def assert_result_failed(result, classification: str) -> None:
    assert result.status == "failed"
    assert result.classification == classification


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
    "provider_results",
    "set_optional_env",
]
