from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import nodes_of, offender_at, offenders_over_functions
from tests.rules import Bites, Rule

# The scheduler's delivery sink is not a worker step.
_SINK_PARAM_NAME = "on_result"


_SINK_CLOSURE_NAME = "deliver"


def _is_result_sink_owner(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        (isinstance(arg, ast.arg) and arg.arg == _SINK_PARAM_NAME)
        or (isinstance(arg, ast.Name) and arg.id == _SINK_CLOSURE_NAME)
        for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs)
    ) or any(
        isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef) and inner.name == _SINK_CLOSURE_NAME
        for inner in node.body
    )


def _sink_call_in_try_body(tree: ast.AST, path: pathlib.Path) -> list[str]:
    def check(node):
        if not _is_result_sink_owner(node):
            return []
        found: list[str] = []
        for statement in node.body:
            if not isinstance(statement, ast.Try):
                continue
            # Only the try BODY: a delivery in an ``except`` handler is the required worker-failure path, and
            # one in ``finally``/``else`` is already outside the protected region.
            for protected in statement.body:
                for inner in nodes_of(protected):
                    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)):
                        continue
                    if inner.func.id in {_SINK_PARAM_NAME, _SINK_CLOSURE_NAME}:
                        found.append(
                            offender_at(
                                path,
                                inner,
                                f"{inner.func.id}() inside the worker's try: delivery is not a "
                                f"worker step, and a "
                                f"raising sink must not be answered with a failure envelope",
                            )
                        )
        return found

    return offenders_over_functions(tree, check)


DELIVERY_NEVER_RUNS_INSIDE_THE_WORKER_TRY = Rule(
    name="delivery-never-runs-inside-the-worker-try",
    find=_sink_call_in_try_body,
    summary="Delivery raised from a worker handler",
    bite=lambda: (
        # The handler delivery is REQUIRED (that is the worker's failure path); only the try-body one is the
        # defect, so the count is 1 and not 2.
        Bites(
            "async def run(self, jobs, worker, on_result=None):\n"
            "    async def deliver(index, result):\n"
            "        await on_result(index, result)\n"
            "    try:\n"
            "        result = await worker(job)\n"
            "        await deliver(job.index, result)\n"
            "    except Exception as error:\n"
            "        await deliver(job.index, failed_result(job, error))\n",
            marker="inside the worker's try",
        )
    ),
    clean=lambda: (
        "async def run(self, jobs, worker, on_result=None):\n"
        "    async def deliver(index, result):\n"
        "        await on_result(index, result)\n"
        "    try:\n"
        "        result = await worker(job)\n"
        "    except Exception as error:\n"
        "        await deliver(job.index, failed_result(job, error))\n"
        "        return\n"
        "    await deliver(job.index, result)\n",
        # A function with no sink is not this detector's business, try or not.
        "async def run(worker):\n"
        "    try:\n"
        "        return await worker(job)\n"
        "    except Exception:\n"
        "        return None\n",
    ),
)


def test_result_delivery_never_runs_inside_the_worker_try():
    DELIVERY_NEVER_RUNS_INSIDE_THE_WORKER_TRY.scan()


def test_sink_try_detector_bites():
    DELIVERY_NEVER_RUNS_INSIDE_THE_WORKER_TRY.check_bites()
