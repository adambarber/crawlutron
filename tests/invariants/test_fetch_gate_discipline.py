"""Every fetch executor is reached only under ``request_gate`` (the gated door list).

#328/#344: 92070eb gated the four HTTP doors but left the MCP fetch surface calling
the executors direct, so this enumerates the doors and fails an ungated one.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    call_nodes,
    enclosing_function,
    functions,
    local_nodes,
    offender_at,
    parent_map,
    resolved_call_name,
    resolved_expr_name,
)
from tests.rules import Bites, Rule

# The four fetch executors a door (HTTP or MCP) must reach through request_gate.
_GATED_FETCH_EXECUTORS = frozenset(
    {
        "crawlutron.service.crawl_executor.execute_crawl",
        "crawlutron.service.map_endpoint.map_site",
        "crawlutron.api.render_endpoint.render",
        "crawlutron.search.endpoint.search",
    }
)

# The one executor-calling executor (map_site -> execute_crawl); its callers'
# gating is what counts because reaching map_site through a gate gates its edge.
_GATED_EXECUTOR_NAMES = frozenset(name.rsplit(".", 1)[-1] for name in _GATED_FETCH_EXECUTORS)

# Background workers run the same executor off the readiness path, holding a durable
# job row rather than a slot; they are not doors and need no request_gate.
_GATED_FETCH_BACKGROUND = frozenset(
    {
        "crawlutron/service/async_crawl_runner.py",
        "crawlutron/service/job_runner.py",
        "crawlutron/service/watch_sweep.py",
    }
)

# The MCP admission chokepoint every fetch tool funnels through; its work runs gated.
_MCP_ADMIT_TOOL = "_admit_tool"


def _is_request_gate_with(node: ast.AST, tree: ast.AST) -> bool:
    if not isinstance(node, (ast.With, ast.AsyncWith)):
        return False
    return any(resolved_expr_name(tree, item.context_expr).endswith("request_gate") for item in node.items)


def _gated(
    call: ast.Call,
    tree: ast.AST,
    parents: dict[int, ast.AST],
    admit_tool: frozenset[int],
    admit_tool_gated: bool,
) -> bool:
    node = parents.get(id(call))
    while node is not None:
        if _is_request_gate_with(node, tree):
            return True
        if id(node) in admit_tool and admit_tool_gated:
            return True
        node = parents.get(id(node))
    return False


def _entered_only_through_admit_tool(fn: ast.FunctionDef | ast.AsyncFunctionDef, tree: ast.AST) -> bool:
    """Whether every call of ``fn`` sits inside a ``work=`` lambda of ``_admit_tool``.

    The structural replacement for the old name-check: a helper's executor call
    sits behind a lambda indirection, so the lexical gate walk finds nothing.
    """
    entry_nodes = {id(node) for entry in _work_lambda_entries(tree) for node in local_nodes(entry)}
    if not entry_nodes:
        return False
    for call in call_nodes(tree):
        callee = resolved_call_name(tree, call)
        if callee.rsplit(".", 1)[-1] != fn.name:
            continue
        if id(call) not in entry_nodes:
            return False
    return True


def _work_lambda_entries(tree: ast.AST) -> list[ast.AST]:
    """Every expression passed as ``work=`` to the MCP admit chokepoint."""
    entries: list[ast.AST] = []
    for call in call_nodes(tree):
        if not resolved_call_name(tree, call).endswith(_MCP_ADMIT_TOOL):
            continue
        for keyword in call.keywords:
            if keyword.arg == "work":
                entries.append(keyword.value)
    return entries


def _function_is_gated(fn: ast.AST, tree: ast.AST) -> bool:
    """Whether a function body carries a request_gate (nested defs excluded)."""
    return any(
        isinstance(node, (ast.With, ast.AsyncWith)) and _is_request_gate_with(node, tree)
        for node in local_nodes(fn)
    )


def _admit_tool_gated(tree: ast.AST) -> bool:
    """The MCP chokepoint must itself hold a request_gate or the whole surface leaks."""
    for fn in functions(tree):
        if fn.name == _MCP_ADMIT_TOOL:
            return _function_is_gated(fn, tree)
    return False


def _entered_only_through_gates(fn: ast.FunctionDef | ast.AsyncFunctionDef, tree: ast.AST) -> bool:
    """Whether every in-module call of ``fn`` sits lexically under ``request_gate``.

    A door's private helper (crawl()'s ``_admitted_crawl``) inherits the door's
    gate transitively: the derived fact is the call sites' enclosure, not a name.
    """
    call_sites = [
        call for call in call_nodes(tree) if resolved_call_name(tree, call).rsplit(".", 1)[-1] == fn.name
    ]
    return bool(call_sites) and all(
        _gated(call, tree, parent_map(tree), frozenset(), False) for call in call_sites
    )


def _ungated_fetch_executor_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    parents = parent_map(tree)
    admit_tool = frozenset(
        id(call) for call in call_nodes(tree) if resolved_call_name(tree, call) == _MCP_ADMIT_TOOL
    )
    admit_tool_gated = _admit_tool_gated(tree)
    offenders: list[str] = []
    for call in call_nodes(tree):
        name = resolved_call_name(tree, call)
        if name not in _GATED_FETCH_EXECUTORS:
            continue
        owner = enclosing_function(call, parents)
        # Gated transitively: this owner IS a gated executor (map_site), so its
        # callers' gating is what counts.
        if owner is not None and owner.name in _GATED_EXECUTOR_NAMES:
            continue
        if _gated(call, tree, parents, admit_tool, admit_tool_gated):
            continue
        # A helper behind a work= lambda is gated only when every call of it
        # funnels through the MCP chokepoint; a derived fact, not a name skip.
        if admit_tool_gated and owner is not None and _entered_only_through_admit_tool(owner, tree):
            continue
        # A door's private helper inherits the door's gate; same derived-fact shape.
        if owner is not None and _entered_only_through_gates(owner, tree):
            continue
        offenders.append(
            offender_at(
                path,
                call,
                f"{name} runs without request_gate: an ungated fetch door hides its "
                "work from /ready and the capacity cap",
            )
        )
    return offenders


FETCH_EXECUTORS_REACH_WORK_ONLY_THROUGH_THE_GATE = Rule(
    name="fetch-executors-reach-work-only-through-the-gate",
    find=_ungated_fetch_executor_offenders,
    summary=(
        "a fetch executor (crawl/map/render/search) must be reached only under "
        "crawlutron.service.main_helpers.request_gate, so /ready counts the work and the cap refuses it"
    ),
    skip=_GATED_FETCH_BACKGROUND,
    bite=lambda: Bites(
        "from crawlutron.service.map_endpoint import map_site\n"
        "async def handle_map(payload, settings, shutdown_event):\n"
        "    return await map_site(payload, settings=settings, shutdown_event=shutdown_event)\n",
        "map_site",
    ),
    clean=lambda: (
        "from crawlutron.service.map_endpoint import map_site\n"
        "from crawlutron.service.main_helpers import request_gate\n"
        "async def handle_map(payload, settings):\n"
        '    with request_gate("map_request_rejected", logger=logger):\n'
        "        return await map_site(payload, settings=settings)\n",
        "async def f(payload):\n    return await other_helper(payload)\n",
    ),
)


def test_fetch_executors_reach_work_only_through_the_gate():
    """The MCP door calls execute_crawl/map_site/render/search only under request_gate."""
    FETCH_EXECUTORS_REACH_WORK_ONLY_THROUGH_THE_GATE.scan()
