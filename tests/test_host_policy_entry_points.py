from __future__ import annotations

import ast
import pathlib

import pytest
from chokepoint_walks import REPO_ROOT, app_trees, resolved_call_name
from test_safety_invariants import _local_nodes, _module_definition_index

from app.validation import HOST_POLICY_ENTRY_POINTS

_DECISION_NAMES = frozenset(
    {
        "_validated_url",
        "canonical_page_identity",
        "checked_payload_urls",
        "filter_js_endpoints",
        "normalize_public_http_url",
        "public_url_or_none",
        "safe_public_url_or_none",
        "validate_webhook_url",
        "validate_webhook_url_async",
        "_parse_pinned_off_loop",
    }
)
_MODULE_PREFIXES = ("app.api.", "app.mcp.")


def _module_name(path: pathlib.Path) -> str:
    relative = path.relative_to(REPO_ROOT).as_posix()
    if relative.endswith("/__init__.py"):
        return relative[: -len("/__init__.py")].replace("/", ".")
    return relative[:-3].replace("/", ".")


def _call_edges(
    modules: tuple[tuple[pathlib.Path, ast.Module], ...],
    index: dict[str, list[tuple[pathlib.Path, ast.Module, ast.AST]]],
) -> dict[str, set[str]]:
    edges = {name: set() for name in index}
    for name, candidates in index.items():
        if len(candidates) != 1:
            continue
        _path, tree, node = candidates[0]
        for child in _local_nodes(node):
            if not isinstance(child, ast.Call):
                continue
            resolved = resolved_call_name(tree, child)
            target = index.get(resolved, [])
            if not target and isinstance(child.func, ast.Name):
                matches = [
                    (candidate_name, candidate)
                    for candidate_name, candidate in index.items()
                    if len(candidate) == 1
                    and candidate[0][1] is tree
                    and getattr(candidate[0][2], "name", None) == child.func.id
                ]
                if len(matches) == 1:
                    target = matches[0][1]
            if len(target) == 1 and target[0][2] is not node:
                target_name = next(
                    candidate_name
                    for candidate_name, candidate in index.items()
                    if len(candidate) == 1 and candidate[0] is target[0]
                )
                edges[name].add(target_name)
    return edges


def _mcp_handlers(tree: ast.Module) -> set[str]:
    handlers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "handler" and isinstance(keyword.value, ast.Name):
                handlers.add(keyword.value.id)
    return handlers


def _has_policy_reference(tree: ast.Module, node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id == "HostPolicy":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "HostPolicy":
            return True
        if isinstance(child, ast.Call) and resolved_call_name(tree, child).rsplit(".", 1)[-1] in _DECISION_NAMES:
            return True
    return False


def discover_host_policy_entry_points(
    modules: tuple[tuple[pathlib.Path, ast.Module], ...],
) -> set[str]:
    index = _module_definition_index(modules)
    edges = _call_edges(modules, index)
    reverse = {name: set() for name in index}
    for caller, callees in edges.items():
        for callee in callees:
            reverse[callee].add(caller)
    reachable: set[str] = set()
    pending = [
        name
        for name in index
        if name.rsplit(".", 1)[-1] in _DECISION_NAMES or name.endswith(".SafeUrl.parse")
    ]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(reverse.get(name, ()))
    discovered: set[str] = set()
    for name, candidates in index.items():
        if len(candidates) != 1:
            continue
        path, tree, node = candidates[0]
        module = _module_name(path)
        if not module.startswith(_MODULE_PREFIXES) or name in reachable and name.rsplit(".", 1)[-1] in _DECISION_NAMES:
            continue
        leaf = name.rsplit(".", 1)[-1]
        handler_names = _mcp_handlers(tree)
        if leaf.startswith("_") and leaf not in handler_names:
            continue
        if leaf not in handler_names and not _has_policy_reference(tree, node):
            continue
        if name in reachable or leaf in handler_names:
            discovered.add(f"{path.relative_to(REPO_ROOT).as_posix()}:{leaf}")
    return discovered


def _app_modules() -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    return tuple(app_trees(exclude=frozenset()))


def _table_entries() -> set[str]:
    return set(HOST_POLICY_ENTRY_POINTS)


def test_host_policy_entry_table_matches_ast_discovery():
    assert discover_host_policy_entry_points(_app_modules()) == _table_entries()


def test_host_policy_discovery_rejects_undeclared_entry_point():
    modules = _app_modules() + (
        (
            REPO_ROOT / "app/api/_undeclared_entry.py",
            ast.parse(
                "from app.validation import HostPolicy, public_url_or_none\n"
                "def undeclared_entry(url, *, policy: HostPolicy):\n"
                "    return public_url_or_none(url, policy=policy)\n"
            ),
        ),
    )
    discovered = discover_host_policy_entry_points(modules)
    assert "app/api/_undeclared_entry.py:undeclared_entry" in discovered
    assert discovered - _table_entries()


_BLOCKED_URL = "https://blocked.test/secret"


def _denied_settings():
    from app.settings import CrawlerSettings

    return CrawlerSettings.from_mapping({"BROWSER_CRAWLER_DENIED_HOSTS": "blocked.test"})


def _denied_policy():
    from app.validation import HostPolicy

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


async def _assert_refused(awaitable, effects: dict[str, list]) -> None:
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


async def _probe_crawl_endpoint(monkeypatch):
    import asyncio

    from fastapi import BackgroundTasks

    from app.api.crawl_endpoint import crawl
    from app.api_models import CrawlRequest

    background_tasks = BackgroundTasks()
    effects = {"persisted": [], "queued": background_tasks.tasks}
    job_store = type("Store", (), {"create_single_shot": lambda self, *args: effects["persisted"].append(args)})()
    await _assert_refused(
        crawl(
            CrawlRequest(url=_BLOCKED_URL),
            background_tasks,
            _denied_settings(),
            job_store,
            object(),
            asyncio.Event(),
        ),
        effects,
    )


async def _probe_crawl_executor(monkeypatch):
    from app.api import crawl_executor

    effects = {"fetched": []}

    async def denied_crawl_request(payload, **kwargs):
        if payload.get("url") != _BLOCKED_URL:
            effects["fetched"].append(payload.get("url"))
        return {"results": [{"status": "blocked", "classification": "blocked"}]}

    monkeypatch.setattr(crawl_executor, "crawl_request", denied_crawl_request)
    await _assert_refused(
        crawl_executor.execute_crawl(
            {"url": _BLOCKED_URL},
            settings=_denied_settings(),
            shutdown_event=None,
        ),
        effects,
    )


async def _probe_jobs_router(monkeypatch):
    from app.api import jobs_router
    from app.api_models import JobCreateRequest

    effects = {"persisted": [], "started": []}
    job_store = type(
        "Store",
        (),
        {
            "create": lambda self, *args: effects["persisted"].append(args),
            "load": lambda self, *args: None,
        },
    )()
    monkeypatch.setattr(jobs_router.job_runner, "start_job", lambda *args: effects["started"].append(args))
    await _assert_refused(
        jobs_router.create_job(
            JobCreateRequest(root_url=_BLOCKED_URL),
            job_store=job_store,
            settings=_denied_settings(),
            shutdown_event=None,
        ),
        effects,
    )


async def _probe_map_endpoint(monkeypatch):
    from app.api import map_endpoint
    from app.api_models import MapRequest

    effects = {"fetched": []}

    async def denied_execute(payload, **kwargs):
        if payload.get("url") != _BLOCKED_URL:
            effects["fetched"].append(payload.get("url"))
        return {"results": []}

    monkeypatch.setattr(map_endpoint, "execute_crawl", denied_execute)
    await _assert_refused(
        map_endpoint.map_site(
            MapRequest(url=_BLOCKED_URL),
            settings=_denied_settings(),
            shutdown_event=None,
        ),
        effects,
    )


async def _probe_render(monkeypatch):
    from app.api.render import render_request

    effects = {"worker": []}

    async def worker(job):
        effects["worker"].append(job.url)
        raise AssertionError("blocked render reached the worker")

    await _assert_refused(
        render_request({"url": _BLOCKED_URL}, settings=_denied_settings(), worker=worker),
        effects,
    )


async def _probe_async_runner(monkeypatch):
    import asyncio

    from app.api import async_crawl_runner

    effects = {"webhook": []}
    job = type("Job", (), {"job_id": "job", "results": [], "stats": {}, "error": None})()

    class Store:
        def load(self, request_id):
            return job

        def set_status(self, *args, **kwargs):
            return True

        async def asave(self, value):
            return None

    async def denied_execute(*args, **kwargs):
        return {"results": [], "request_id": "job", "submitted_count": 0, "policy_schema_version": ""}

    class Results:
        def put(self, *args):
            return None

    monkeypatch.setattr(async_crawl_runner, "execute_crawl", denied_execute)
    await _assert_refused(
        async_crawl_runner.run_async_crawl(
            "job",
            {"url": _BLOCKED_URL},
            _BLOCKED_URL,
            _denied_settings(),
            Store(),
            Results(),
            asyncio.Event(),
            urls=[_BLOCKED_URL],
        ),
        effects,
    )


async def _probe_response_shaping(monkeypatch):
    from app.api.response_shaping import frontier_link_url

    async def invoke():
        return frontier_link_url({"normalized_url": _BLOCKED_URL}, policy=_denied_policy())

    await _assert_refused(invoke(), {"wire": []})


async def _probe_webhook(monkeypatch):
    from app.api import webhooks

    effects = {"wire": []}

    async def forbidden_delivery(*args, **kwargs):
        effects["wire"].append(args)

    monkeypatch.setattr(webhooks, "deliver_pinned_webhook", forbidden_delivery)
    await _assert_refused(
        webhooks.post_webhook(_BLOCKED_URL, {"status": "accepted"}, host_policy=_denied_policy()),
        effects,
    )


async def _probe_webhook_delivery(monkeypatch):
    import httpx

    from app.api import webhooks

    effects = {"wire": []}

    def forbidden_client(*args, **kwargs):
        effects["wire"].append(args)
        raise AssertionError("blocked webhook reached the wire")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden_client)
    await _assert_refused(
        webhooks.deliver_pinned_webhook(
            _BLOCKED_URL,
            b"{}",
            host_policy=_denied_policy(),
        ),
        effects,
    )


def _blocked_http(*args, **kwargs):
    from fastapi import HTTPException

    raise HTTPException(status_code=422, detail="blocked by host policy")


async def _probe_mcp_crawl(monkeypatch):
    from app.mcp import tools

    monkeypatch.setattr(tools, "execute_crawl", _blocked_http)
    await _assert_refused(tools._handle_crawl({"url": _BLOCKED_URL}, _denied_settings(), None), {})


async def _probe_mcp_map(monkeypatch):
    from app.mcp import tools

    monkeypatch.setattr(tools, "map_site", _blocked_http)
    await _assert_refused(tools._handle_map({"url": _BLOCKED_URL}, _denied_settings(), None), {})


async def _probe_mcp_render(monkeypatch):
    from app.mcp import tools

    monkeypatch.setattr(tools, "get_browser_pool", lambda: object())
    monkeypatch.setattr(tools, "render_endpoint_handler", _blocked_http)
    await _assert_refused(tools._handle_render({"url": _BLOCKED_URL}, _denied_settings(), None), {})


async def _probe_mcp_search(monkeypatch):
    from app.mcp import tools

    monkeypatch.setattr(tools, "search_endpoint_handler", _blocked_http)
    await _assert_refused(tools._handle_search({"query": "blocked"}, _denied_settings(), None), {})


_PROBES = {
    "app/api/crawl_endpoint.py:crawl": _probe_crawl_endpoint,
    "app/api/crawl_executor.py:execute_crawl": _probe_crawl_executor,
    "app/api/jobs_router.py:create_job": _probe_jobs_router,
    "app/api/map_endpoint.py:map_site": _probe_map_endpoint,
    "app/api/render.py:render_request": _probe_render,
    "app/api/async_crawl_runner.py:run_async_crawl": _probe_async_runner,
    "app/api/response_shaping.py:frontier_link_url": _probe_response_shaping,
    "app/api/webhooks.py:post_webhook": _probe_webhook,
    "app/api/webhooks.py:deliver_pinned_webhook": _probe_webhook_delivery,
    "app/mcp/tools.py:_handle_crawl": _probe_mcp_crawl,
    "app/mcp/tools.py:_handle_map": _probe_mcp_map,
    "app/mcp/tools.py:_handle_render": _probe_mcp_render,
    "app/mcp/tools.py:_handle_search": _probe_mcp_search,
}


@pytest.mark.parametrize("entry", sorted(_table_entries()))
async def test_host_policy_entry_point_refuses(entry, monkeypatch):
    await _PROBES[entry](monkeypatch)


def test_host_policy_probe_table_matches_entry_table():
    assert set(_PROBES) == _table_entries()


async def test_render_probe_fails_when_policy_enforcement_is_removed(monkeypatch):
    from app.api import render
    from app.crawl import payload_helpers

    def allow(url, *, policy):
        return url

    monkeypatch.setattr(payload_helpers, "normalize_public_http_url", allow)
    monkeypatch.setattr(render, "normalize_public_http_url", allow)
    with pytest.raises(AssertionError):
        await _probe_render(monkeypatch)
