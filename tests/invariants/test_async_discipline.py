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
    async_function_defs,
    attribute_calls,
    calls_to_any,
    functions,
    nodes_of,
    offender_at,
    offenders_over_functions,
)
from tests.invariants._shared import app_names
from tests.rules import Bites, Rule, Trips

# ---------------------------------------------------------------------------
# The admission sizing weights are one value
#
# ``build_admission_policy`` and ``max_http_concurrency_for`` each took the same
# four sizing weights as keyword-only parameters, and the duplication was
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
    from app.resource_pressure import TaskRamBudget

    return TaskRamBudget


def _admission_weights_are_known() -> None:
    """The weights this rule bans as keywords are read from the dataclass, not listed.

    A weight added to ``TaskRamBudget`` is covered here the day it lands, and a
    dataclass emptied or renamed leaves this failing rather than leaving the rule
    matching nothing -- which would read as the property holding.
    """
    weights = {field.name for field in dataclasses.fields(_task_ram_budget())}
    assert weights, "TaskRamBudget declared no weights; this sweep would be a no-op"


ADMISSION_WEIGHTS_ARE_KEYWORDS = (
    "from app.resource_pressure import build_admission_policy\n"
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
        "from app.resource_pressure import TaskRamBudget, build_admission_policy\n"
        "def build(limit, pool_size):\n"
        "    return build_admission_policy(limit, pool_size=pool_size,"
        " max_concurrent_pages=3, budget=TaskRamBudget(doc_workers=2))\n",
        # The computation itself is not this guard's subject.
        "from app.resource_pressure import max_http_concurrency_for\n"
        "def ceiling(limit, pool_size):\n"
        "    return max_http_concurrency_for(limit, pool_size, doc_workers=2)\n",
        "from app.resource_pressure import build_admission_policy\n"
        "def build(limit, pool_size):\n"
        "    return build_admission_policy(limit, pool_size=pool_size, max_concurrent_pages=3)\n",
    ),
)


def test_admission_sizing_reaches_the_policy_as_one_budget():
    """No caller re-spells the four sizing weights as keywords to the policy builder."""
    ADMISSION_BUDGET_ARRIVES_AS_A_BUDGET.scan()


def test_admission_budget_detector_bites():
    """The flat keyword fires on every weight; the budget form is clean.

    ``max_http_concurrency_for`` is deliberately NOT in the clean set's
    counterpart: it is the per-call computation the budget is bound for, so it
    still takes the four weights flat and its own call inside the policy function
    is the sanctioned forwarding. What is banned is a *caller* re-spelling them.
    """
    ADMISSION_BUDGET_ARRIVES_AS_A_BUDGET.check_bites()


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
        # The release is the expensive one to lose, in both its spellings: a finally
        # and a return.
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


def test_readiness_slots_are_admitted_and_released_by_the_gate():
    READINESS_SLOTS_GO_THROUGH_THE_GATE.scan()


def test_readiness_counter_detector_bites():
    """A hand-rolled admit, and a hand-rolled release, both fire; the gate stays clean."""
    READINESS_SLOTS_GO_THROUGH_THE_GATE.check_bites()


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
        # The accessor, bare and qualified: the import style is the module's choice
        # and must not decide whether the gate is recognised. The set is what the
        # accessor consults, so a direct membership test is legitimate rather than a
        # bypass.
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


def test_whole_site_visited_commit_is_transient_gated():
    """A transiently-failed whole-site URL is re-queued, never counted done."""
    WHOLE_SITE_VISIT_IS_TRANSIENT_GATED.scan()


def test_whole_site_visited_gate_detector_bites():
    """An unguarded whole-site visited commit fires; both gate spellings stay clean.

    The message must name the seam to route through, or a red detector is only as
    useful as the guess it forces -- which is why this is a :class:`Bites` proof
    rather than a bare firing check.
    """
    WHOLE_SITE_VISIT_IS_TRANSIENT_GATED.check_bites()


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


def _stores_still_have_sync_methods() -> None:
    """The trigger set is derived, so prove it is not empty.

    ``load`` and ``aload`` are the pair that makes the whole rule meaningful: a
    store whose methods were all made async would leave this detector matching
    nothing, and matching nothing reads exactly like a store that is never
    misused. Named rather than counted, because the count could be satisfied by
    one synchronous method nobody calls in an async frame.
    """
    sync_methods = _sync_store_methods()
    assert "load" in sync_methods["JobStore"] and "aload" not in sync_methods["JobStore"], (
        f"JobStore's sync/async pair moved: {sorted(sync_methods['JobStore'])[:8]}. This rule's "
        f"trigger set is derived from the class, so it is re-derived rather than listed -- "
        f"which means a renamed method makes the rule match nothing instead of reporting."
    )


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
    for node in async_function_defs(tree):
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


STORE_CALLS_ON_THE_LOOP = Rule(
    name="store-calls-on-the-loop",
    find=_sync_store_call_offenders,
    summary="A store call on the event loop stalls every in-flight request",
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
    ),
    clean=lambda: (
        "async def get_crawl_result(job_store, result_store):\n"
        "    stored = await result_store.aget(request_id)\n"
        "    job = await job_store.aload(request_id)\n",
        # A sync frame is not the hazard: startup recovery before the loop exists
        # and test setup are both entitled to the synchronous methods.
        "def seed(job_store):\n    return job_store.load(job_id)\n",
    ),
)


def test_no_sync_store_method_is_called_from_an_async_frame():
    """A store call on the event loop stalls every in-flight request.

    The proof covers the four ways a frame names a store -- unannotated by
    convention, annotated, constructed, and awaited -- because a detector that
    resolved only one of them would pass on the other three while reporting the
    property as held.
    """
    STORE_CALLS_ON_THE_LOOP.scan()


def test_sync_store_call_detector_bites():
    STORE_CALLS_ON_THE_LOOP.check_bites()


#: The driver's method that runs a scripted action chain. Named once and f-string
#: into every message below: this rule keys on a method name in the production
#: tree, and when the lifecycle split renamed it the key, the three messages and
#: the two proof sources were left pointing at a method that no longer existed --
#: so the rule scanned the tree, matched nothing, and reported a green that meant
#: nothing. One constant makes the rename a one-line edit.
_ACTION_CHAIN = "run_action_chain"


def _action_chain_still_exists() -> None:
    """The premise: the rule is armed on a name the production tree must have.

    The bite proves the detector fires; only this proves it has anything to fire
    on. Those are different claims, and the gap between them is where the rule
    spent a commit reporting green while governing nothing: the lifecycle split
    renamed the method, the detector kept comparing against the old spelling,
    and the bite stayed green throughout because the bite plants the name the
    *detector* reads rather than the name the *tree* has.

    A premise is the framework's slot for exactly this ("asserting the rule's
    trigger set is non-empty"), and the tree-wide arming sweep cannot cover this
    rule: ``run_action_chain`` is public, so it has no private spelling to go red
    on.
    """
    assert _ACTION_CHAIN in app_names(), (
        f"{_ACTION_CHAIN} is not spelled anywhere in app/, so the rule matching calls to it "
        "matches nothing and reports green while policing no call site. Rename the key in "
        "the detector, or drop the rule if the chain moved out of scope."
    )


def _action_chain_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    """Every call that must carry the chain's deadline must carry one.

    ``BrowserDriver.run_action_chain`` takes ``budget_seconds`` as an OPTIONAL
    argument so a test can drive the raw action executor without standing up a
    render budget. An optional deadline is a hole exactly as wide as the next
    caller who omits it, so the signature stays permissive and this sweep
    carries the invariant: production always passes the budget.
    """
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


def test_the_action_chain_is_always_invoked_with_a_budget():
    ACTION_CHAIN_ALWAYS_CARRIES_A_BUDGET.scan()


def test_action_chain_budget_detector_bites():
    ACTION_CHAIN_ALWAYS_CARRIES_A_BUDGET.check_bites()


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
    def check(node):
        if not _is_result_sink_owner(node):
            return []
        found: list[str] = []
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
        # The handler delivery is REQUIRED (that is the worker's failure path);
        # only the try-body one is the defect, so the count is 1 and not 2.
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
