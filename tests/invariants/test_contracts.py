from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    assignment_targets,
    call_nodes,
    calls_to_any,
    nodes_of,
    offender_at,
    relative_posix,
    try_blocks,
    writes,
)
from tests.rules import Bites, Rule

"""Payloads are built once and strategy runners run guarded."""


def _job_engine_payload_chokepoint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    has_builder = any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_job_engine_payload"
        for node in nodes_of(tree)
    )
    if relative != "app/api/job_runner.py" and not has_builder:
        return []
    execute_names = {"_execute_crawl", "_real_execute_crawl"}
    builder_names = {"_job_engine_payload"}
    for node in writes(tree):
        value = node.value
        targets = assignment_targets(node)
        if isinstance(value, ast.Name) and value.id in execute_names:
            execute_names.update(target.id for target in targets if isinstance(target, ast.Name))
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in builder_names
        ):
            builder_names.update(target.id for target in targets if isinstance(target, ast.Name))
    offenders: list[str] = []
    for call in calls_to_any(tree, execute_names):
        payload = (
            call.args[0]
            if call.args
            else next(
                (keyword.value for keyword in call.keywords if keyword.arg in {"payload", "data"}), None
            )
        )
        if isinstance(payload, ast.Name) and payload.id in builder_names:
            continue
        if (
            isinstance(payload, ast.Call)
            and isinstance(payload.func, ast.Name)
            and payload.func.id in builder_names
        ):
            continue
        offenders.append(offender_at(path, node, "_execute_crawl payload bypasses _job_engine_payload"))
    return offenders


JOB_ENGINE_PAYLOAD_USES_SINGLE_BUILDER = Rule(
    name="job-engine-payload-uses-single-builder",
    find=_job_engine_payload_chokepoint_offenders,
    summary=(
        "Build whole-site engine payloads only with _job_engine_payload, "
        "so a new call site to _execute_crawl cannot silently skip the builder"
    ),
    proof_path=REPO_ROOT / "app" / "api" / "job_runner.py",
    bite=lambda: (
        Bites(
            "async def run(job, batch):\n"
            "    payload = {'urls': batch, 'policy': dict(job.policy)}\n"
            "    await _execute_crawl(payload)\n",
            "payload bypasses _job_engine_payload",
        ),
        # An alias of the executor is the same bypass spelled a second way.
        Bites(
            "async def run(job, batch):\n"
            "    execute = _execute_crawl\n"
            "    await execute({'urls': batch, 'policy': {}})\n",
            "payload bypasses _job_engine_payload",
        ),
    ),
    clean=lambda: (
        "async def run(job, batch):\n"
        "    payload = _job_engine_payload(job, batch)\n"
        "    await _execute_crawl(payload)\n",
    ),
)


def test_job_engine_payload_uses_single_builder():
    JOB_ENGINE_PAYLOAD_USES_SINGLE_BUILDER.scan()


def test_job_engine_payload_chokepoint_detector_bites():
    JOB_ENGINE_PAYLOAD_USES_SINGLE_BUILDER.check_bites()


# The strategy chain's raise-chokepoint.
_STRATEGY_RUNNERS = frozenset({"_run_http_strategy", "_run_browser_strategy"})


def _guarded_node_ids(tree: ast.AST) -> set[int]:
    """Identity of every node in a ``try`` body that has an ``except Exception``.

    Only the guarded ``body`` counts. A call in an ``except``/``else``/
    ``finally`` clause is not protected by its own try, so including those
    would open exactly the hole this detector closes.
    """
    guarded: set[int] = set()
    for node in try_blocks(tree):
        if not any(
            isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}
            for handler in node.handlers
        ):
            continue
        for statement in node.body:
            guarded.update(id(child) for child in nodes_of(statement))
    return guarded


def _runner_call_name(node: ast.Call) -> str:
    """A strategy runner's name, whether it was called bare or as a method.

    Resolving both spellings is what keeps the invariant attached to the CODE
    rather than to one particular refactor's shape. A detector that matched only
    ``ast.Name`` reported a clean tree at the exact moment the runner became a
    method -- the refactor that cannot weaken the invariant is the one most
    likely to make it silently unenforced, because nothing about the change looks
    like touching it.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _unguarded_strategy_runner_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    guarded = _guarded_node_ids(tree)
    offenders: list[str] = []
    for node in call_nodes(tree):
        name = _runner_call_name(node)
        if name in _STRATEGY_RUNNERS and id(node) not in guarded:
            offenders.append(offender_at(path, node, f"{name} invoked outside a try/except Exception"))
    return offenders


STRATEGY_RUNNERS_ARE_CALLED_UNDER_A_GUARD = Rule(
    name="strategy-runners-are-called-under-a-guard",
    find=_unguarded_strategy_runner_offenders,
    summary=(
        "A strategy that raises must become a classified failed step that the ladder "
        "moves on from, not an exception that escapes the chain; call each runner "
        "inside a try with an except Exception"
    ),
    proof_path=REPO_ROOT / "app" / "crawl" / "strategy_chain.py",
    bite=lambda: (
        # Both runners called bare, outside any try: each unguarded call is a finding.
        Bites(
            "async def _execute_strategy(context, strategy):\n"
            "    if strategy in HTTP_STRATEGIES:\n"
            "        return await _run_http_strategy(context, strategy)\n"
            "    return await _run_browser_strategy(context, strategy)\n",
            "invoked outside a try/except Exception",
            count=2,
        ),
        # A narrow handler is not a guard: a bare except TypeError lets every other raise through, which is
        # the shape the bug shipped as.
        Bites(
            "async def _execute_strategy(context, strategy):\n"
            "    try:\n"
            "        return await _run_http_strategy(context, strategy)\n"
            "    except TypeError:\n"
            "        return None\n",
            "invoked outside a try/except Exception",
        ),
        # The runner reached as a METHOD is the same sink.
        Bites(
            "class Run:\n"
            "    async def execute(self, strategy):\n"
            "        return await self._run_browser_strategy(strategy)\n",
            "invoked outside a try/except Exception",
        ),
    ),
    clean=lambda: (
        # The guarded form parses and is spared.
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    except Exception as error:\n"
        "        return engine_result_from_exception(error=error)\n",
        # The guard is satisfied by an enclosing try too, not only a sibling one.
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        if strategy:\n"
        "            return await _run_browser_strategy(context, strategy)\n"
        "    except Exception:\n"
        "        return None\n",
        # A method form under the guard stays clean.
        "class Run:\n"
        "    async def execute(self, strategy):\n"
        "        try:\n"
        "            return await self._run_browser_strategy(strategy)\n"
        "        except Exception as error:\n"
        "            return engine_result_from_exception(error=error)\n",
    ),
)


def test_every_strategy_runner_call_is_guarded():
    STRATEGY_RUNNERS_ARE_CALLED_UNDER_A_GUARD.scan()


def test_strategy_runner_guard_detector_bites():
    STRATEGY_RUNNERS_ARE_CALLED_UNDER_A_GUARD.check_bites()
