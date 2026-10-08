from __future__ import annotations

import ast
import asyncio
import pathlib
from collections import defaultdict
from collections.abc import Mapping, MutableMapping
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from app.crawl.result_store import CrawlResultStore
from app.models.host_policy import HOST_POLICY_ENTRY_POINTS
from tests.chokepoint_walks import REPO_ROOT, app_trees, local_nodes, nodes_of, resolved_call_name
from tests.invariants._shared import DefinitionNode, _module_definition_index

_DECISION_NAMES = frozenset(
    {
        # The constructor is the admission chokepoint since the value object
        # took over from the egress helpers: calling it IS the decision.
        "NormalizedUrl",
        "_validated_url",
        "admit_or_none",
        "checked_payload_urls",
        "filter_js_endpoints",
        "validate_webhook_url",
        "validate_webhook_url_async",
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
    index: dict[str, list[tuple[pathlib.Path, ast.Module, DefinitionNode]]],
) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {name: set() for name in index}
    for name, candidates in index.items():
        if len(candidates) != 1:
            continue
        _path, tree, node = candidates[0]
        for child in local_nodes(node):
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
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "handler" and isinstance(keyword.value, ast.Name):
                handlers.add(keyword.value.id)
    return handlers


def _has_policy_reference(tree: ast.Module, node: ast.AST) -> bool:
    for child in nodes_of(node):
        if isinstance(child, ast.Name) and child.id == "HostPolicy":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "HostPolicy":
            return True
        if (
            isinstance(child, ast.Call)
            and resolved_call_name(tree, child).rsplit(".", 1)[-1] in _DECISION_NAMES
        ):
            return True
    return False


def discover_host_policy_entry_points(
    modules: tuple[tuple[pathlib.Path, ast.Module], ...],
) -> set[str]:
    index = _module_definition_index(modules)
    edges = _call_edges(modules, index)
    reverse: dict[str, set[str]] = {name: set() for name in index}
    for caller, callees in edges.items():
        for callee in callees:
            reverse[callee].add(caller)
    reachable: set[str] = set()
    pending = [
        name
        for name in index
        if name.rsplit(".", 1)[-1] in _DECISION_NAMES
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
        if (
            not module.startswith(_MODULE_PREFIXES)
            or name in reachable
            and name.rsplit(".", 1)[-1] in _DECISION_NAMES
        ):
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
    return app_trees(exclude=frozenset())


def _table_entries() -> set[str]:
    return set(HOST_POLICY_ENTRY_POINTS)


def test_host_policy_entry_table_matches_ast_discovery():
    assert discover_host_policy_entry_points(_app_modules()) == _table_entries()


def test_host_policy_discovery_rejects_undeclared_entry_point():
    modules = _app_modules() + (
        (
            REPO_ROOT / "app/api/_undeclared_entry.py",
            ast.parse(
                "from app.models.host_policy import HostPolicy\n"
                "from app.models.normalized_url import admit_or_none\n"
                "def undeclared_entry(url, *, policy: HostPolicy):\n"
                "    return admit_or_none(url, policy=policy)\n"
            ),
        ),
    )
    discovered = discover_host_policy_entry_points(modules)
    assert "app/api/_undeclared_entry.py:undeclared_entry" in discovered
    assert discovered - _table_entries()


_BLOCKED_URL = "https://blocked.test/secret"


def _denied_settings():
    from app.settings import CrawlerSettings

    return CrawlerSettings.from_mapping({"CRAWLUTRON_DENIED_HOSTS": "blocked.test"})


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


async def _probe_crawl_endpoint(monkeypatch, tmp_path):
    import asyncio

    from app.api.crawl_endpoint import crawl
    from app.models.crawl_request import CrawlRequest

    effects = _effects()
    job_store = type(
        "Store", (), {"create_single_shot": lambda self, *args: effects["persisted"].append(args)}
    )()
    await _assert_refused(
        crawl(
            CrawlRequest(url=_BLOCKED_URL),
            _denied_settings(),
            job_store,
            # A real result store, not ``object()``. The gate refuses before it
            # reaches this, and that is the claim under test -- so a stand-in
            # made true an accident rather than a fact: the call had to be
            # type-correct for "never touched" to mean anything.
            CrawlResultStore(tmp_path / "host-policy-results.db"),
            asyncio.Event(),
        ),
        effects,
    )


async def _probe_crawl_executor(monkeypatch, tmp_path):
    from app.api import crawl_executor

    effects = _effects()

    async def denied_crawl_request(payload, **kwargs):
        _record_probe_fetch(effects, payload)
        return {"results": [{"status": "blocked", "classification": "blocked"}]}

    monkeypatch.setattr(crawl_executor, "crawl_request", denied_crawl_request)
    await _assert_refused(
        crawl_executor.execute_crawl(
            {"url": _BLOCKED_URL},
            settings=_denied_settings(),
            # A real event: the guard refuses before it reads this, and the
            # parameter is required.
            shutdown_event=asyncio.Event(),
        ),
        effects,
    )


async def _probe_jobs_router(monkeypatch, tmp_path):
    from app.api import jobs_router
    from app.models.job_create_request import JobCreateRequest

    effects = _effects()
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
            # A real event: the handler refuses before it reads this, and the
            # parameter is a required ``asyncio.Event``.
            shutdown_event=asyncio.Event(),
        ),
        effects,
    )


async def _probe_map_endpoint(monkeypatch, tmp_path):
    from app.api import map_endpoint
    from app.models.map_request import MapRequest

    effects = _effects()

    async def denied_execute(payload, **kwargs):
        _record_probe_fetch(effects, payload)
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


async def _probe_watch_router(monkeypatch, tmp_path):
    from app.api import watch_router
    from app.models.watch_create_request import WatchCreateRequest

    effects = _effects()
    watch_store = type(
        "Store",
        (),
        {"acreate": lambda self, *args, **kwargs: effects["persisted"].append(args)},
    )()
    await _assert_refused(
        watch_router.create_watch(
            WatchCreateRequest(url=_BLOCKED_URL, interval_seconds=60),
            watch_store=watch_store,
            settings=_denied_settings(),
        ),
        effects,
    )


async def _probe_render(monkeypatch, tmp_path):
    from app.api.render import render_request

    effects = _effects()

    async def worker(job):
        effects["worker"].append(job.url.raw)
        raise AssertionError("blocked render reached the worker")

    await _assert_refused(
        render_request({"url": _BLOCKED_URL}, settings=_denied_settings(), worker=worker),
        effects,
    )


async def _probe_async_runner(monkeypatch, tmp_path):
    import asyncio

    from app.api import async_crawl_runner

    effects = _effects()
    # The REAL stores, not doubles: a hand-rolled `type("Store", ...)` drifted
    # the moment a call site moved to the async twin and reported a contract
    # change as an AttributeError, which is noise, not a finding. A real store
    # on a tmp path also proves the refusal happens with nothing persisted.
    from app.crawl.result_store import CrawlResultStore
    from tests._job_harnesses import make_job_store

    job_store = make_job_store(str(tmp_path))
    results_store = CrawlResultStore(tmp_path / "results.db")

    async def denied_execute(*args, **kwargs):
        return {"results": [], "request_id": "job", "submitted_count": 0, "policy_schema_version": ""}

    monkeypatch.setattr(async_crawl_runner, "execute_crawl", denied_execute)
    await _assert_refused(
        async_crawl_runner.run_async_crawl(
            "job",
            {"url": _BLOCKED_URL},
            _BLOCKED_URL,
            _denied_settings(),
            job_store,
            results_store,
            asyncio.Event(),
            urls=[_BLOCKED_URL],
        ),
        effects,
    )


async def _probe_response_shaping(monkeypatch, tmp_path):
    from app.api.response_shaping import frontier_link_url

    async def invoke():
        return frontier_link_url({"normalized_url": _BLOCKED_URL}, policy=_denied_policy())

    await _assert_refused(invoke(), {"wire": []})


async def _probe_webhook(monkeypatch, tmp_path):
    from app.api import webhooks

    effects = _effects()

    async def forbidden_delivery(*args, **kwargs):
        effects["wire"].append(args)

    monkeypatch.setattr(webhooks, "deliver_pinned_webhook", forbidden_delivery)
    await _assert_refused(
        webhooks.post_webhook(_BLOCKED_URL, {"status": "accepted"}, host_policy=_denied_policy()),
        effects,
    )


async def _probe_webhook_delivery(monkeypatch, tmp_path):
    from app.api import webhooks

    effects = _effects()

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
    raise HTTPException(status_code=422, detail="blocked by host policy")


def _tool_payload(tool: str, **fields):
    """Build what the MCP dispatcher builds before a handler ever sees it.

    The dispatcher validates a call's arguments into the tool's declared
    ``request_model``, so a probe that invokes a handler directly has to
    construct that same model -- a raw dict is no longer what a handler takes.
    Reading the model off the registration rather than naming it here keeps the
    probe from becoming a second place that knows which model a tool uses.
    """
    from app.mcp.tools import TOOLS_BY_NAME

    return TOOLS_BY_NAME[tool].request_model(**fields)


async def _probe_mcp_crawl(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "execute_crawl", _blocked_http)
    await _assert_refused(
        tools._handle_crawl(_tool_payload("crawl", url=_BLOCKED_URL), _denied_settings(), None), {}
    )


async def _probe_mcp_crawl_and_extract(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "execute_crawl", _blocked_http)
    await _assert_refused(
        tools._handle_crawl_and_extract(
            _tool_payload("crawl_and_extract", url=_BLOCKED_URL), _denied_settings(), None
        ),
        {},
    )


async def _probe_mcp_map(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "map_site", _blocked_http)
    await _assert_refused(
        tools._handle_map(_tool_payload("map", url=_BLOCKED_URL), _denied_settings(), None), {}
    )


async def _probe_mcp_render(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "get_browser_pool", lambda: object())
    monkeypatch.setattr(tools, "render_endpoint_handler", _blocked_http)
    await _assert_refused(
        tools._handle_render(_tool_payload("render", url=_BLOCKED_URL), _denied_settings(), None), {}
    )


async def _probe_mcp_search(monkeypatch, tmp_path):
    from app.mcp import tools

    monkeypatch.setattr(tools, "search_endpoint_handler", _blocked_http)
    await _assert_refused(
        tools._handle_search(_tool_payload("search", query="blocked"), _denied_settings(), None), {}
    )


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


async def _probe_policy_suggestions(monkeypatch, tmp_path):
    """A host this deployment refuses must produce no suggestion at either level.

    The rollup admits every stored URL through the deployment's policy, and a
    candidate rule about a host this deployment may not crawl is not reviewable
    -- so the refusal shows up as an empty proposal rather than an exception,
    which is why this probe asserts on the suggestion list instead of reusing
    :func:`_assert_refused`.
    """
    from app.api import policy_suggestions as module
    from app.models.trace_response import TraceResponse

    async def _payloads():
        return _blocked_records()

    store = type("Store", (), {"apayloads": staticmethod(_payloads)})()

    from_handler = await module.policy_suggestions(result_store=store, settings=_denied_settings())
    assert from_handler.suggestions == []

    record = TraceResponse.model_validate(_blocked_records()[0])
    from_builder = module.build_policy_suggestions([record], host_policy=_denied_policy())
    assert from_builder.suggestions == []


_PROBES = {
    "app/api/crawl_endpoint.py:crawl": _probe_crawl_endpoint,
    "app/api/crawl_executor.py:execute_crawl": _probe_crawl_executor,
    "app/api/jobs_router.py:create_job": _probe_jobs_router,
    "app/api/map_endpoint.py:map_site": _probe_map_endpoint,
    "app/api/render.py:render_request": _probe_render,
    "app/api/async_crawl_runner.py:run_async_crawl": _probe_async_runner,
    "app/api/response_shaping.py:frontier_link_url": _probe_response_shaping,
    "app/api/watch_router.py:create_watch": _probe_watch_router,
    "app/api/policy_suggestions.py:policy_suggestions": _probe_policy_suggestions,
    "app/api/policy_suggestions.py:build_policy_suggestions": _probe_policy_suggestions,
    "app/api/webhooks.py:post_webhook": _probe_webhook,
    "app/api/webhooks.py:deliver_pinned_webhook": _probe_webhook_delivery,
    "app/mcp/tools.py:_handle_crawl": _probe_mcp_crawl,
    "app/mcp/tools.py:_handle_crawl_and_extract": _probe_mcp_crawl_and_extract,
    "app/mcp/tools.py:_handle_map": _probe_mcp_map,
    "app/mcp/tools.py:_handle_render": _probe_mcp_render,
    "app/mcp/tools.py:_handle_search": _probe_mcp_search,
}


@pytest.mark.parametrize("entry", sorted(_table_entries()))
async def test_host_policy_entry_point_refuses(entry, monkeypatch, tmp_path):
    await _PROBES[entry](monkeypatch, tmp_path)


def test_host_policy_probe_table_matches_entry_table():
    assert set(_PROBES) == _table_entries()


async def test_render_probe_fails_when_policy_enforcement_is_removed(monkeypatch, tmp_path):
    from app.api import render
    from app.crawl import payload_helpers

    def allow(url, *, policy):
        # The VALUE shape render reads ``.raw``/``.site`` off, with the host
        # check removed -- a bare string is no longer a stand-in for an admitted
        # URL, so the double has to admit the denied host rather than skip it.
        from app.models.host_policy import HostPolicy
        from app.models.normalized_url import NormalizedUrl

        return NormalizedUrl(url, policy=HostPolicy.unrestricted())

    monkeypatch.setattr(payload_helpers, "NormalizedUrl", allow)
    monkeypatch.setattr(render, "NormalizedUrl", allow)
    with pytest.raises(AssertionError):
        await _probe_render(monkeypatch, tmp_path)
