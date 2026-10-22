"""Sync work does not happen on the event loop, and budgets are passed not defaulted.

A blocking store call in an async frame, a sink that never runs, or a crawl that
grows the frontier without the admission funnel are shapes that only show under load.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib

from tests.chokepoint_walks import (
    async_function_defs,
    attribute_calls,
    calls_to_any,
    functions,
    nodes_of,
    offender_at,
)
from tests.invariants._shared import app_names
from tests.invariants._store_receivers import (
    _helper_store_bites,
    _module_store_globals,
    _store_receivers,
)
from tests.rules import Bites, Registry, Rule, Trips

# The weights are now one frozen ``resource_pressure.TaskRamBudget`` that the caller builds and the policy
# function hands on unchanged. The drift this guards is a caller going back to the flat list.
_ADMISSION_POLICY = "build_admission_policy"


def _admission_budget_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A ``build_admission_policy`` call carrying a sizing weight as a keyword."""
    weights = {field.name for field in dataclasses.fields(_task_ram_budget())}
    offenders: list[str] = []
    for node in calls_to_any(tree, {_ADMISSION_POLICY}):
        for keyword in node.keywords:
            if keyword.arg not in weights:
                continue
            offenders.append(
                offender_at(
                    path,
                    node,
                    f"{_ADMISSION_POLICY}(..., {keyword.arg}={ast.unparse(keyword.value)}, ...) "
                    f"spells a sizing weight directly; pass a resource_pressure.TaskRamBudget "
                    f"so the policy is built from the same weights it reports",
                )
            )
    return offenders


def _task_ram_budget():
    """The budget dataclass, imported late so the module docstring stays the first prose."""
    from app.runtime.resource_pressure import TaskRamBudget

    return TaskRamBudget


def _admission_weights_are_known() -> None:
    """The weights this rule bans as keywords are read from the dataclass.

    A weight added to TaskRamBudget is covered the day it lands; a dataclass
    emptied leaves this failing rather than the rule matching nothing."""
    weights = {field.name for field in dataclasses.fields(_task_ram_budget())}
    assert weights, "TaskRamBudget declared no weights; this sweep would be a no-op"


ADMISSION_WEIGHTS_ARE_KEYWORDS = (
    "from app.runtime.resource_pressure import build_admission_policy\n"
    "def build(limit, pool_size):\n"
    "    return build_admission_policy(limit, pool_size=pool_size,"
    " max_concurrent_pages=3, doc_workers=2)\n"
)


ADMISSION_BUDGET_ARRIVES_AS_A_BUDGET = Rule(
    name="admission-weights-arrive-as-a-budget",
    find=_admission_budget_offenders,
    summary=(
        "admission sizing weights must arrive as a TaskRamBudget, not as four"
        " keywords that can drift from the budget the policy reports"
    ),
    premise=_admission_weights_are_known,
    bite=lambda: Bites(ADMISSION_WEIGHTS_ARE_KEYWORDS, marker="doc_workers=2"),
    clean=lambda: (
        "from app.runtime.resource_pressure import TaskRamBudget, build_admission_policy\n"
        "def build(limit, pool_size):\n"
        "    return build_admission_policy(limit, pool_size=pool_size,"
        " max_concurrent_pages=3, budget=TaskRamBudget(doc_workers=2))\n",
        # The computation itself is not this guard's subject.
        "from app.runtime.resource_pressure import max_http_concurrency_for\n"
        "def ceiling(limit, pool_size):\n"
        "    return max_http_concurrency_for(limit, pool_size, doc_workers=2)\n",
        "from app.runtime.resource_pressure import build_admission_policy\n"
        "def build(limit, pool_size):\n"
        "    return build_admission_policy(limit, pool_size=pool_size, max_concurrent_pages=3)\n",
    ),
)


# --------------------------------------------------------------------------- Endpoint readiness gate (the
# rejection tail) ``request_gate`` (app/api/main_helpers.py) owns the whole gated tail: admit, reject

_READINESS_GATE_EXEMPT = (
    # The owner itself.
    "app/api/main_helpers.py",
)


def _unsanctioned_readiness_counter_offenders(tree, path):
    """Flag a direct readiness-slot admit/release outside the gate chokepoint."""
    return [
        offender_at(path, call, f"{callee.attr}() outside app.api.main_helpers.request_gate")
        for call, callee in attribute_calls(tree)
        if callee.attr in {"begin_request", "end_request"}
    ]


READINESS_SLOTS_GO_THROUGH_THE_GATE = Rule(
    name="readiness-slots-go-through-the-gate",
    find=_unsanctioned_readiness_counter_offenders,
    summary=(
        "take a readiness slot with app.api.main_helpers.request_gate; a hand-written "
        "admit/release is how a rejected request holds its slot for the life of the process"
    ),
    skip=_READINESS_GATE_EXEMPT,
    bite=lambda: (
        Bites("def f():\n    readiness_state.begin_request()\n", "begin_request"),
        # The release is the expensive one to lose, in both its spellings: a finally and a return.
        Bites(
            "def f():\n    try:\n        pass\n    finally:\n        readiness_state.end_request()\n",
            "end_request",
        ),
        Bites("def f():\n    return READINESS_STATE.end_request()\n", "end_request"),
    ),
    clean=lambda: (
        "async def f(payload):\n"
        '    with request_gate("crawl_request_rejected", logger=logger):\n'
        "        return await execute(payload)\n",
        "def f():\n    get_metrics().observe_crawl(errors=1)\n",
    ),
)


# --------------------------------------------------------------------------- Whole-site frontier must not
# drain on transient failure ---------------------------------------------------------------------------
_TRANSIENCE_GATE_NAMES = ("is_transient_classification", "TRANSIENT_CLASSIFICATIONS")


def _consults_transience_verdict(func: ast.AST) -> bool:
    """Whether a function asks the taxonomy a transience question, either way.

    Matches the bare-name and qualified-attribute spellings, so the gate is
    recognised however the module chose to import it.
    """
    return any(
        (isinstance(n, ast.Name) and n.id in _TRANSIENCE_GATE_NAMES)
        or (isinstance(n, ast.Attribute) and n.attr in _TRANSIENCE_GATE_NAMES)
        for n in nodes_of(func)
    )


def _ungated_job_visited_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Flag committing a whole-site URL to ``job.visited`` without the gate.

    Committing a transient-failed URL is what drains a whole-site frontier to
    a seed-only COMPLETED; the per-request local set is a different thing."""
    offenders: list[str] = []
    for func in functions(tree):
        commits = [
            n
            for n in nodes_of(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "add"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "visited"
            and isinstance(n.func.value.value, ast.Name)
            and n.func.value.value.id == "job"
        ]
        if not commits:
            continue
        if not _consults_transience_verdict(func):
            offenders.append(
                offender_at(
                    path,
                    commits[0],
                    f"job.visited.add() without a transience "
                    f"gate -- route it through {_TRANSIENCE_GATE_NAMES[0]}()",
                )
            )
    return offenders


WHOLE_SITE_VISIT_IS_TRANSIENT_GATED = Rule(
    name="whole-site-visit-is-transient-gated",
    find=_ungated_job_visited_offenders,
    summary=(
        "gate every job.visited.add on is_transient_classification() "
        "so transient failures are re-queued, not marked visited"
    ),
    bite=lambda: Bites(
        "def run(job):\n    for url in batch:\n        job.visited.add(url)\n",
        marker="is_transient_classification",
    ),
    clean=lambda: (
        # The accessor, bare and qualified: the import style is the module's choice and must not decide
        # whether the gate is recognised.
        "def run(job, outcome):\n    for url in batch:\n"
        "        if is_transient_classification(outcome):\n            continue\n"
        "        job.visited.add(url)\n",
        "def run(job, outcome):\n    for url in batch:\n"
        "        if constants.is_transient_classification(outcome):\n            continue\n"
        "        job.visited.add(url)\n",
        "def run(job, outcome):\n    for url in batch:\n"
        "        if outcome in TRANSIENT_CLASSIFICATIONS:\n            continue\n"
        "        job.visited.add(url)\n",
    ),
)


# The receiver-resolution vocabulary (params, binds, conventional names, self.attr,
# module-level store globals, one hop of helper indirection) lives next door.


def _sync_store_methods() -> dict[str, frozenset[str]]:
    """Store class name -> its public synchronous method names; derived not listed."""
    import app.storage.caller_key_store as caller_key_store
    import app.storage.coordination_cookies as coordination_cookies
    import app.storage.coordination_counters as coordination_counters
    import app.storage.job_store as job_store
    import app.storage.result_store as result_store
    import app.storage.watch_store as watch_store

    modules = {
        "JobStore": job_store,
        "CrawlResultStore": result_store,
        "WatchStore": watch_store,
        "CoordinationCounters": coordination_counters,
        "SharedCookieStore": coordination_cookies,
        "CallerKeyStore": caller_key_store,
    }
    sync: dict[str, frozenset[str]] = {}
    for name, module in modules.items():
        store_class = getattr(module, name)
        sync[name] = frozenset(
            member
            for member in dir(store_class)
            if not member.startswith("_")
            and callable(getattr(store_class, member, None))
            and not inspect.iscoroutinefunction(getattr(store_class, member))
        )
    return sync


def _stores_still_have_sync_methods() -> None:
    """The trigger set is derived, so prove it is not empty.

    A store whose methods were all made async would leave this detector matching
    nothing; named, not counted, so one unused method cannot satisfy it.
    """
    sync_methods = _sync_store_methods()
    assert "load" in sync_methods["JobStore"] and "aload" not in sync_methods["JobStore"], (
        f"JobStore's sync/async pair moved: {sorted(sync_methods['JobStore'])[:8]}. This rule's "
        f"trigger set is derived from the class, so it is re-derived rather than listed -- "
        f"which means a renamed method makes the rule match nothing instead of reporting."
    )


def _sync_store_call_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    sync_methods = _sync_store_methods()
    offenders: list[str] = []
    for node in async_function_defs(tree):
        receivers = _store_receivers(node, tree)
        receivers.update(_module_store_globals(tree))
        for inner in nodes_of(node):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
                continue
            owner = receivers.get(ast.unparse(inner.func.value))
            if owner is not None and inner.func.attr in sync_methods[owner]:
                msg = f"{ast.unparse(inner.func.value)}.{inner.func.attr} is synchronous"
                offenders.append(offender_at(path, inner, msg))
        offenders.extend(_helper_store_bites(node, tree, sync_methods, path))
    return offenders


STORE_CALLS_ON_THE_LOOP = Rule(
    name="store-calls-on-the-loop",
    find=_sync_store_call_offenders,
    summary=(
        "A store call on the event loop stalls every in-flight request. One hop of "
        "same-module helper indirection is followed (a sync helper called by an async "
        "frame that reaches a sync store method through it); cross-module helpers are a "
        "deliberate out-of-scope boundary named in the detector's docstring."
    ),
    premise=_stores_still_have_sync_methods,
    bite=lambda: (
        Trips(
            "async def get_crawl_result(job_store, result_store):\n"
            "    stored = result_store.get(request_id)\n"
            "    job = job_store.load(request_id)\n",
            count=2,
        ),
        Bites("async def runner(store: JobStore):\n    return store.list_jobs()\n", "store.list_jobs"),
        Bites(
            "async def lifespan():\n"
            "    store = JobStore(path, policy=policy)\n"
            "    return store.delete(job_id)\n",
            "store.delete",
        ),
        Bites(
            "from app.storage.coordination_cookies import SharedCookieStore\n"
            "class Jar:\n"
            "    def __init__(self, path):\n"
            "        self._store = SharedCookieStore(path)\n"
            "    async def headers_for(self, url):\n"
            "        return self._store.load('ns', url)\n",
            "self._store.load",
        ),
        # One level of helper indirection must not hide the hazard: the async caller
        # reaches the sync store method through the sync helper it calls on the loop.
        Bites(
            "def _fetch_all(store: JobStore):\n"
            "    return store.list_jobs()\n"
            "async def handler(store: JobStore):\n"
            "    return _fetch_all(store)\n",
            "_fetch_all",
        ),
        # A module-level store global is a receiver every function in the module sees.
        Bites(
            "_SHARED = JobStore(path, policy=policy)\nasync def handler():\n    return _SHARED.list_jobs()\n",
            "_SHARED.list_jobs",
        ),
        # Module-global through helper: receiver set composes module globals with
        # helper bindings, so _h(_STORE) reaches the sync method on the loop.
        Bites(
            "_SHARED = JobStore(path, policy=policy)\n"
            "def _h(store):\n"
            "    return store.list_jobs()\n"
            "async def handler():\n"
            "    return _h(_SHARED)\n",
            "_h",
        ),
        # Keyword-argument passing escapes a positional-only probe: _h(store=store) reaches
        # the sync store method through the helper on the loop.
        Bites(
            "def _h(store):\n"
            "    return store.list_jobs()\n"
            "async def handler(store: JobStore):\n"
            "    return _h(store=store)\n",
            "_h",
        ),
    ),
    clean=lambda: (
        "async def get_crawl_result(job_store, result_store):\n"
        "    stored = await result_store.aget(request_id)\n"
        "    job = await job_store.aload(request_id)\n",
        # A sync frame is not the hazard: startup recovery before the loop exists and test setup are both
        # entitled to the synchronous methods.
        "def seed(job_store):\n    return job_store.load(job_id)\n",
        # The helper itself is entitled to the sync methods when it is not on the loop:
        # an async twin helper awaited by the async caller keeps the store call off it.
        "async def handler(store: JobStore):\n"
        "    return await _fetch_all(store)\n"
        "async def _fetch_all(store: JobStore):\n"
        "    return await store.aload(job_id)\n",
        # Module-global through helper: async twin keeps the call off the loop.
        "_SHARED = JobStore(path, policy=policy)\n"
        "async def _h(store):\n"
        "    return await store.aload(job_id)\n"
        "async def handler():\n"
        "    return await _h(_SHARED)\n",
        # Keyword-argument passing: async twin keeps the call off the loop.
        "async def _h(store):\n"
        "    return await store.aload(job_id)\n"
        "async def handler(store):\n"
        "    return await _h(store=store)\n",
    ),
)


#: The driver's method that runs a scripted action chain. One constant makes the rename a one-line edit.
_ACTION_CHAIN = "run_action_chain"


def _action_chain_still_exists() -> None:
    """The premise: the rule is armed on a name the production tree must have.

    The bite proves the detector fires; this proves it has anything to fire on.
    A renamed method kept the bite green while the rule governed nothing."""
    assert _ACTION_CHAIN in app_names(), (
        f"{_ACTION_CHAIN} is not spelled anywhere in app/, so the rule matching calls to it "
        "matches nothing and reports green while policing no call site. Rename the key in "
        "the detector, or drop the rule if the chain moved out of scope."
    )


def _action_chain_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Every call that must carry the chain's deadline must carry one.

    The signature stays permissive so a test can drive the raw executor; this
    sweep carries the invariant that production always passes the budget."""
    offenders: list[str] = []
    for node, func in attribute_calls(tree):
        if func.attr != _ACTION_CHAIN:
            continue
        if any(keyword.arg == "budget_seconds" for keyword in node.keywords):
            continue
        offenders.append(
            offender_at(
                path,
                node,
                f"{_ACTION_CHAIN} runs without a budget, so a "
                "wedged action holds the browser pool slot for as long as it likes",
            )
        )
    return offenders


ACTION_CHAIN_ALWAYS_CARRIES_A_BUDGET = Rule(
    name="action-chain-always-carries-a-budget",
    find=_action_chain_offenders,
    premise=_action_chain_still_exists,
    summary=(
        f"{_ACTION_CHAIN} runs without a budget, so a wedged action holds the "
        "browser pool slot for as long as it likes"
    ),
    bite=lambda: Bites(
        f"async def render(self, page, actions):\n    await self.{_ACTION_CHAIN}(page, actions)\n",
        marker=_ACTION_CHAIN,
    ),
    clean=lambda: (
        "async def render(self, page, actions, budget):\n"
        f"    await self.{_ACTION_CHAIN}(page, actions, budget_seconds=budget)\n",
        f"async def render(self):\n    await self.{_ACTION_CHAIN}_forever()\n",
    ),
)


RULES = Registry(
    (
        ADMISSION_BUDGET_ARRIVES_AS_A_BUDGET,
        READINESS_SLOTS_GO_THROUGH_THE_GATE,
        WHOLE_SITE_VISIT_IS_TRANSIENT_GATED,
        STORE_CALLS_ON_THE_LOOP,
        ACTION_CHAIN_ALWAYS_CARRIES_A_BUDGET,
    )
)


def test_no_rule_violations():
    RULES.check_all_clean()


def test_every_rule_bites():
    """Every rule must reject what it forbids, and spare what it resembles."""
    RULES.check_all_bite()
