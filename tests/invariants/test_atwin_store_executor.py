"""Every async store call runs on the dedicated store executor, never the default.

``atwin`` is the single chokepoint for every async store call (all stores, all
methods). Routing it through ``asyncio.to_thread`` funnels store work onto the
default executor (min(32, cpu + 4)), which crawl saturation can exhaust and
queue every store call. ``atwin`` must instead run on ``_STORE_EXECUTOR``, a
bounded dedicated pool, so store saturation can never starve the rest of the
loop. The RLock already serializes in-process store access, so the executor
bound is modest.
"""

import ast

from tests._walk_assertions import assert_detectors_clean
from tests.chokepoint_walks import REPO_ROOT, attribute_call_on, call_offenders, parsed

_STORE = REPO_ROOT / "crawlutron" / "storage" / "coordination_store.py"


def test_async_store_calls_run_on_a_dedicated_executor() -> None:
    assert_detectors_clean(
        parsed(_STORE),
        path=_STORE,
        detectors=(_asyncio_to_thread_offenders, _atwin_executor_offenders),
    )


def _is_asyncio_to_thread(call: ast.Call) -> bool:
    return attribute_call_on(call, "asyncio", "to_thread")


def _is_run_in_executor_without_store(call: ast.Call) -> bool:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "run_in_executor"):
        return False
    return not any(isinstance(a, ast.Name) and a.id == "_STORE_EXECUTOR" for a in call.args)


def _asyncio_to_thread_offenders(tree: ast.Module) -> list[str]:
    return call_offenders(
        tree,
        _STORE,
        _is_asyncio_to_thread,
        "asyncio.to_thread funnels store work to the default executor",
    )


def _atwin_executor_offenders(tree: ast.Module) -> list[str]:
    return call_offenders(
        tree,
        _STORE,
        _is_run_in_executor_without_store,
        "run_in_executor must use _STORE_EXECUTOR, not the default",
    )
