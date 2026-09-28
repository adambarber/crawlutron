"""Sync work does not happen on the event loop, and budgets are passed not defaulted.

A blocking store call inside an async frame, a sink that never runs because it
sat inside the worker's own try, or a crawler that grows the frontier without
the admission funnel are all shapes that only show up under load."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib

from tests.chokepoint_walks import (
    REPO_ROOT,
    SANCTIONED,
    app_offenders,
    app_trees,
    assert_fires_once,
    assert_stays_clean,
    attribute_calls,
    calls_to_any,
    functions,
    nodes_of,
    nodes_of_type,
    offender_at,
)
from tests.invariants._shared import assert_trees_clean

# ---------------------------------------------------------------------------
# The admission sizing weights are one value
#
# ``_build_admission_policy`` and ``max_http_concurrency_for`` each took the same
# four sizing weights as keyword-only parameters, and the pair jscpd reported was
# those two signatures rather than any logic. The weights are now one frozen
# ``resource_pressure.TaskRamBudget`` that the caller builds and the policy
# function hands on unchanged.
#
# The drift this guards is a caller going back to the flat list. Nothing about
# the arithmetic stops that: a ``doc_workers=2`` keyword would work again the
# moment the parameter was re-added, and the weight would be reserved outside the
# budget the policy is built from -- the two lists drifting apart again, which is
# the shape the consolidation removed. So the budget is the only way in, and
# this says so over the whole tree rather than over the two modules involved.
# ---------------------------------------------------------------------------

# The four weights, read from the dataclass rather than transcribed: a weight
# added to TaskRamBudget is covered here the day it lands.
_ADMISSION_POLICY = "_build_admission_policy"


def _admission_budget_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """A ``_build_admission_policy`` call carrying a sizing weight as a keyword."""
    from app.resource_pressure import TaskRamBudget

    weights = {field.name for field in dataclasses.fields(TaskRamBudget)}
    assert weights, "TaskRamBudget declared no weights; this sweep would be a no-op"
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


def test_admission_sizing_reaches_the_policy_as_one_budget():
    """No caller re-spells the four sizing weights as keywords to the policy builder."""
    assert_trees_clean(
        _admission_budget_offenders,
        "admission sizing weights must arrive as a TaskRamBudget, not as four"
        " keywords that can drift from the budget the policy reports",
    )


def test_admission_budget_detector_bites():
    """The flat keyword fires on every weight; the budget form is clean.

    ``max_http_concurrency_for`` is deliberately NOT in this set: it is the
    per-call computation the budget is bound for, so it still takes the four
    weights flat and its own call inside the policy function is the sanctioned
    forwarding. What is banned is a *caller* re-spelling them.
    """
    assert_fires_once(
        _admission_budget_offenders,
        {
            "from app.resource_pressure import _build_admission_policy\n"
            "def build(limit, pool_size):\n"
            "    return _build_admission_policy(limit, pool_size=pool_size,"
            " max_concurrent_pages=3, doc_workers=2)\n": "doc_workers=2",
        },
        REPO_ROOT / "app" / "_synthetic.py",
    )
    assert_stays_clean(
        _admission_budget_offenders,
        (
            "from app.resource_pressure import TaskRamBudget, _build_admission_policy\n"
            "def build(limit, pool_size):\n"
            "    return _build_admission_policy(limit, pool_size=pool_size,"
            " max_concurrent_pages=3, budget=TaskRamBudget(doc_workers=2))\n",
            # The computation itself is not this guard's subject.
            "from app.resource_pressure import max_http_concurrency_for\n"
            "def ceiling(limit, pool_size):\n"
            "    return max_http_concurrency_for(limit, pool_size, doc_workers=2)\n",
            "from app.resource_pressure import _build_admission_policy\n"
            "def build(limit, pool_size):\n"
            "    return _build_admission_policy(limit, pool_size=pool_size, max_concurrent_pages=3)\n",
        ),
        REPO_ROOT / "app" / "_synthetic.py",
    )


# ---------------------------------------------------------------------------
# Endpoint readiness gate (the rejection tail)
#
# ``request_gate`` (app/api/main_helpers.py) owns the whole gated tail: admit,
# reject (operator warning + the shared 4xx), release. Spelled per endpoint the
# three obligations were independently droppable, and the expensive one to lose
# is the release -- a rejected request that skips ``end_request`` holds its
# readiness slot for the life of the process. Pinning the counters to one module
# makes "an endpoint cannot forget to release" structural: a new gated endpoint
# has to enter the gate, and a re-spelled tail has no chokepoint to drift from.
# ---------------------------------------------------------------------------

_READINESS_GATE_EXEMPT = (
    # The owner itself.
    "app/api/main_helpers.py",
    # PENDING: the fourth gated endpoint, outside app/api. It admits and releases
    # the same slot but has no ValueError rejection to delegate -- it answers 503
    # and 502 from provider failures, which is not what request_gate converts --
    # so it takes the counters directly. Migrating it means teaching the gate
    # those codes or splitting admission from rejection; either way the entry
    # below should go with the migration.
    "app/search/endpoint.py",
)


def _unsanctioned_readiness_counter_offenders(tree, path):
    """Flag a direct readiness-slot admit/release outside the gate chokepoint."""
    return [
        offender_at(path, call, f"{callee.attr}() outside app.api.main_helpers.request_gate")
        for call, callee in attribute_calls(tree)
        if callee.attr in {"begin_request", "end_request"}
    ]


def test_readiness_slots_are_admitted_and_released_by_the_gate():
    offenders = app_offenders(_unsanctioned_readiness_counter_offenders, skip=_READINESS_GATE_EXEMPT)
    assert not offenders, (
        "take a readiness slot with app.api.main_helpers.request_gate; a hand-written "
        "admit/release is how a rejected request holds its slot for the life of the "
        "process: " + ", ".join(offenders)
    )


def test_readiness_counter_detector_bites():
    """A hand-rolled admit, and a hand-rolled release, both fire; the gate stays clean."""
    fires = {
        "def f():\n    readiness_state.begin_request()\n": "begin_request",
        "def f():\n    try:\n        pass\n    finally:\n        readiness_state.end_request()\n":
        "end_request",
        "def f():\n    return READINESS_STATE.end_request()\n": "end_request",
    }
    assert_fires_once(
        _unsanctioned_readiness_counter_offenders, fires, REPO_ROOT / "app" / "api" / "_synthetic.py"
    )
    assert_stays_clean(
        _unsanctioned_readiness_counter_offenders,
        (
            "async def f(payload):\n"
            '    with request_gate("crawl_request_rejected", logger=logger):\n'
            "        return await execute(payload)\n",
            "def f():\n    get_metrics().observe_crawl(errors=1)\n",
        ),
        REPO_ROOT / "app" / "api" / "_synthetic.py",
    )


# ---------------------------------------------------------------------------
# Whole-site frontier must not drain on transient failure
# ---------------------------------------------------------------------------


# Both accepted spellings of a transience gate, kept as one tuple so the
# detector and its failure message cannot drift apart. The accessor is the
# preferred seam; the set is what the accessor consults, so a direct membership
# test is legitimate rather than a bypass. A third spelling must be added here
# deliberately, not discovered by a test going red.
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
    """Flag committing a *whole-site* URL to ``job.visited`` without the gate.

    A URL that fails for a transient reason (network/dns/tls/timeout/crash) must
    be re-queued, not marked visited: committing it is what let a network outage
    drain a whole-site frontier to a seed-only ``COMPLETED``. Keyed on the
    persisted ``job.visited`` receiver so the local per-request ``visited`` set
    (recursive/fetch_helpers) — a different thing, where retrying the same failed
    URL within one crawl is pointless — is not matched.
    """
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


def test_whole_site_visited_commit_is_transient_gated():
    """A transiently-failed whole-site URL is re-queued, never counted done."""
    assert_trees_clean(
        _ungated_job_visited_offenders,
        "gate every job.visited.add on is_transient_classification() "
        "so transient failures are re-queued, not marked visited",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_whole_site_visited_gate_detector_bites():
    """An unguarded whole-site visited commit fires; both gate spellings stay clean."""
    bad = ast.parse("def run(job):\n    for url in batch:\n        job.visited.add(url)\n")
    offenders = _ungated_job_visited_offenders(bad, REPO_ROOT / "app" / "_synthetic.py")
    assert len(offenders) == 1, offenders
    # The message must name the seam to route through, or a red detector is only
    # as useful as the guess it forces.
    assert "is_transient_classification" in offenders[0], offenders

    # The accessor, bare and qualified: the import style is the module's choice
    # and must not decide whether the gate is recognised.
    for gate in (
        "if is_transient_classification(outcome):\n            continue",
        "if constants.is_transient_classification(outcome):\n            continue",
        "if outcome in TRANSIENT_CLASSIFICATIONS:\n            continue",
    ):
        good = ast.parse(
            f"def run(job, outcome):\n    for url in batch:\n        {gate}\n        job.visited.add(url)\n"
        )
        assert not _ungated_job_visited_offenders(good, REPO_ROOT / "app" / "_synthetic.py"), gate


# The store-offload invariant: inside an async def, a store is reached through
# its async twin. The sink is a direct call to a SYNCHRONOUS store method; the
# requirement is that no async frame in app/ makes one. Tree-wide, because the
# next handler, runner, or lifespan step is where it comes back.
_STORE_CLASSES = ("JobStore", "CrawlResultStore")


# The two stores are injected unannotated (FastAPI ``Depends``, lifespan
# globals, runner arguments), so a type-annotation-only resolution would see
# nothing. These names are the project's own convention for the injected
# receiver; a frame that binds one of them holds a store.
_CONVENTIONAL_STORE_NAMES = {"job_store": "JobStore", "result_store": "CrawlResultStore"}


def _sync_store_methods() -> dict[str, frozenset[str]]:
    """Store class name -> its public synchronous method names.

    Derived from the classes rather than listed here, so a method added to a
    store is covered by this detector the day it lands and an async twin added
    later drops out of the set with nobody editing this test. Importing the two
    store modules is safe: the schema runs when a store is CONSTRUCTED, not at
    import.
    """
    from app.crawl.job_store import JobStore
    from app.crawl.result_store import CrawlResultStore

    derived: dict[str, frozenset[str]] = {}
    for class_name, store_class in (("JobStore", JobStore), ("CrawlResultStore", CrawlResultStore)):
        derived[class_name] = frozenset(
            member
            for member in dir(store_class)
            if not member.startswith("_")
            and callable(getattr(store_class, member, None))
            and not inspect.iscoroutinefunction(getattr(store_class, member))
        )
    return derived


def _store_receivers(function: ast.AST) -> dict[str, str]:
    """Name -> store class, for the store-typed bindings of one function."""
    receivers: dict[str, str] = {}
    for node in nodes_of(function):
        if isinstance(node, ast.arg) and node.annotation is not None:
            annotation = ast.unparse(node.annotation)
            if annotation in _STORE_CLASSES:
                receivers[node.arg] = annotation
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Call):
                    continue
                constructor = ast.unparse(node.value.func)
                if constructor in _STORE_CLASSES:
                    receivers[target.id] = constructor
    receivers.update(_CONVENTIONAL_STORE_NAMES)
    return receivers


def _sync_store_call_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    sync_methods = _sync_store_methods()
    offenders: list[str] = []
    for node in nodes_of_type(tree, ast.AsyncFunctionDef):
        receivers = _store_receivers(node)
        for inner in nodes_of(node):
            if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)):
                continue
            owner = receivers.get(ast.unparse(inner.func.value))
            if owner is not None and inner.func.attr in sync_methods[owner]:
                offenders.append(
                    offender_at(
                        path,
                        inner,
                        f"{ast.unparse(inner.func.value)}.{inner.func.attr} "
                        f"is synchronous; await the async twin",
                    )
                )
    return offenders


def test_no_sync_store_method_is_called_from_an_async_frame():
    assert_trees_clean(
        _sync_store_call_offenders,
        "A store call on the event loop stalls every in-flight request",
        trees=app_trees(exclude=SANCTIONED),
    )


def _action_chain_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Every call that must carry the chain's deadline must carry one.

    ``BrowserRenderer._run_action_chain`` takes ``budget_seconds`` as an
    OPTIONAL argument so a test can drive the raw action executor without
    standing up a render budget. An optional deadline is a hole exactly as
    wide as the next caller who omits it, so the signature stays permissive and
    this sweep carries the invariant: production always passes the budget.
    """
    offenders: list[str] = []
    for node, func in attribute_calls(tree):
        if func.attr != "_run_action_chain":
            continue
        if any(keyword.arg == "budget_seconds" for keyword in node.keywords):
            continue
        offenders.append(
            offender_at(
                path,
                node,
                "_run_action_chain runs without a budget, so a "
                "wedged action holds the browser pool slot for as long as it likes",
            )
        )
    return offenders


def test_the_action_chain_is_always_invoked_with_a_budget():
    offenders = app_offenders(_action_chain_offenders)
    assert not offenders, "; ".join(offenders)


def test_action_chain_budget_detector_bites():
    path = REPO_ROOT / "app" / "crawl" / "strategies" / "browser.py"
    unbudgeted = ast.parse(
        "async def render(self, page, actions):\n    await self._run_action_chain(page, actions)\n"
    )
    assert len(_action_chain_offenders(unbudgeted, path)) == 1
    budgeted = ast.parse(
        "async def render(self, page, actions, budget):\n"
        "    await self._run_action_chain(page, actions, budget_seconds=budget)\n"
    )
    assert not _action_chain_offenders(budgeted, path)
    unrelated = ast.parse("async def render(self):\n    await self._run_action_chain_forever()\n")
    assert not _action_chain_offenders(unrelated, path)


def test_sync_store_call_detector_bites():
    path = REPO_ROOT / "app" / "api" / "crawl_endpoint.py"
    sync_methods = _sync_store_methods()
    assert "load" in sync_methods["JobStore"] and "aload" not in sync_methods["JobStore"]
    on_the_loop = ast.parse(
        "async def get_crawl_result(job_store, result_store):\n"
        "    stored = result_store.get(request_id)\n"
        "    job = job_store.load(request_id)\n"
    )
    assert len(_sync_store_call_offenders(on_the_loop, path)) == 2
    annotated = ast.parse("async def runner(store: JobStore):\n    return store.list_jobs()\n")
    assert len(_sync_store_call_offenders(annotated, path)) == 1
    constructed = ast.parse(
        "async def lifespan():\n    store = JobStore(path, policy=policy)\n    return store.delete(job_id)\n"
    )
    assert len(_sync_store_call_offenders(constructed, path)) == 1
    compliant = ast.parse(
        "async def get_crawl_result(job_store, result_store):\n"
        "    stored = await result_store.aget(request_id)\n"
        "    job = await job_store.aload(request_id)\n"
    )
    assert not _sync_store_call_offenders(compliant, path)
    # A sync frame is not the hazard: startup recovery before the loop exists
    # and test setup are both entitled to the synchronous methods.
    sync_frame = ast.parse("def seed(job_store):\n    return job_store.load(job_id)\n")
    assert not _sync_store_call_offenders(sync_frame, path)


# The scheduler's delivery sink is not a worker step. A pipeline that hands the
# scheduler an ``on_result`` sink (or wraps one locally) must deliver AFTER the
# worker's try/except, or a sink that raises is answered as a failed page: the
# domain's breaker is charged, a fabricated envelope is delivered through the
# same broken sink, and gather discards what is left silently.
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
    offenders: list[str] = []
    for node in functions(tree):
        if not _is_result_sink_owner(node):
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Try):
                continue
            # Only the try BODY: a delivery in an ``except`` handler is the
            # required worker-failure path, and one in ``finally``/``else`` is
            # already outside the protected region.
            for protected in statement.body:
                for inner in nodes_of(protected):
                    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)):
                        continue
                    if inner.func.id in {_SINK_PARAM_NAME, _SINK_CLOSURE_NAME}:
                        offenders.append(
                            offender_at(
                                path,
                                inner,
                                f"{inner.func.id}() inside the worker's try: delivery is not a "
                                f"worker step, and a "
                                f"raising sink must not be answered with a failure envelope",
                            )
                        )
    return offenders


def test_result_delivery_never_runs_inside_the_worker_try():
    assert_trees_clean(
        _sink_call_in_try_body,
        "Delivery raised from a worker handler",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_sink_try_detector_bites():
    path = REPO_ROOT / "app" / "scheduler.py"
    contaminated = ast.parse(
        "async def run(self, jobs, worker, on_result=None):\n"
        "    async def deliver(index, result):\n"
        "        await on_result(index, result)\n"
        "    try:\n"
        "        result = await worker(job)\n"
        "        await deliver(job.index, result)\n"
        "    except Exception as error:\n"
        "        await deliver(job.index, failed_result(job, error))\n"
    )
    # The handler delivery is REQUIRED (that is the worker's failure path); only
    # the try-body one is the defect.
    assert len(_sink_call_in_try_body(contaminated, path)) == 1
    clean = ast.parse(
        "async def run(self, jobs, worker, on_result=None):\n"
        "    async def deliver(index, result):\n"
        "        await on_result(index, result)\n"
        "    try:\n"
        "        result = await worker(job)\n"
        "    except Exception as error:\n"
        "        await deliver(job.index, failed_result(job, error))\n"
        "        return\n"
        "    await deliver(job.index, result)\n"
    )
    assert not _sink_call_in_try_body(clean, path)
    # A function with no sink is not this detector's business, try or not.
    unrelated = ast.parse(
        "async def run(worker):\n"
        "    try:\n"
        "        return await worker(job)\n"
        "    except Exception:\n"
        "        return None\n"
    )
    assert not _sink_call_in_try_body(unrelated, path)
