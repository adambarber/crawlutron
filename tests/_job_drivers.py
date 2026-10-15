import asyncio
import sqlite3
from collections.abc import Callable, Collection
from contextlib import contextmanager
from typing import Any

from app.admission.policy import PolicyLike
from app.models.crawl_result import EngineCrawlResult
from app.models.result_record import ProducerEnvelope
from app.storage.job_store import FrontierEntry, JobStatus
from tests._policy_factories import canonical


async def _create_api_job(client, root_url: str, execute, policy: PolicyLike | None = None):
    from app.api import job_runner

    job_runner.install_execute_crawl(execute)
    response = await post_api_job(client, root_url, policy)
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _seed_job(
    job_store,
    root_url: str,
    policy: PolicyLike | None = None,
    *,
    frontier: list[str] | None = None,
    in_flight: list[str] | None = None,
    running: bool = False,
    on_seeded: Callable[[Any, Any], None] | None = None,
):
    job = job_store.create(root_url, policy if policy is not None else {})
    if frontier is not None:
        job.frontier = [FrontierEntry(url=canonical(url), depth=0) for url in frontier]
    if in_flight is not None:
        job.in_flight = [FrontierEntry(url=canonical(url), depth=0) for url in in_flight]
    job_store.save(job)
    if running:
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    if on_seeded is not None:
        on_seeded(job, job_store)
    return job


async def run_to_terminal(job_store, job_id: str) -> Any:
    """Block until ``job_id`` settles; return the finished row.

    This is the one place that knows what "done" means, so the wait and the load
    are stated once. The row is re-read after the wait rather than taken from the
    wait itself: a status CAS and the body that records the batch are separate
    writes, and handing back a row that disagrees with the state the wait
    confirmed would be a lie the caller cannot detect.
    """
    from tests._job_harnesses import _SETTLED_STATUSES, _wait_status

    status = await _wait_status(job_store, job_id, _SETTLED_STATUSES)
    finished = job_store.load(job_id)
    assert finished is not None, f"job {job_id} reported {status} with no row"
    assert finished.status.value == status, (
        f"job {job_id} settled {status} but the row it handed back reads "
        f"{finished.status.value}: the terminal CAS landed before the batch did"
    )
    return finished


async def run_api_job(client, job_store, root_url: str, execute, policy: PolicyLike | None = None):
    """Create a job through the API and drive it to terminal; return the row."""

    return await run_to_terminal(job_store, await _create_api_job(client, root_url, execute, policy))


async def run_scripted_api_job(
    client,
    job_store,
    root_url: str,
    results: list[EngineCrawlResult],
    policy: PolicyLike | None = None,
):
    """``run_api_job`` for the common case: a scripted engine over canned results.

    Building the engine is the decision, and it was re-spelled as a
    ``ScriptedExecute(results=...)`` argument at every job test that only cares
    about the job. Routing it through here means a change to how a scripted
    engine is constructed cannot land in one test and miss the other eleven.
    """
    from tests._job_harnesses import ScriptedExecute

    return await run_api_job(client, job_store, root_url, ScriptedExecute(results=results), policy)


def drive_job(job_id, job_store, settings=None, shutdown_event=None):
    """Run ``run_job`` for one already-persisted job; return the task.

    ``run_job``'s call shape -- id, store, settings, shutdown event -- is what
    every runner-lifecycle test ends on, and ``CrawlerSettings.from_mapping({})``
    was spelled out at each site. A settings default that changes, or a fourth
    argument, is then a four-edit change, and one of the four is this harness:
    the one that silently stops exercising the same call the tests do.

    Returned as a task rather than awaited, because the tests that need to cancel
    or wait on a *running* job have to hold the task to do it.
    """
    from app.api import job_runner
    from app.config.settings import CrawlerSettings

    return asyncio.ensure_future(
        job_runner.run_job(
            job_id,
            job_store,
            CrawlerSettings.from_mapping({}) if settings is None else settings,
            asyncio.Event() if shutdown_event is None else shutdown_event,
        )
    )


async def run_seeded_job(job, job_store, execute):
    """Drive an already-persisted job in-line to terminal; return the row."""
    from app.api import job_runner

    job_runner.install_execute_crawl(execute)
    # A row a crash left RUNNING restarts the way the service restarts it: the recovery rewind first, because
    # the runner's claim only accepts PENDING.
    await job_runner.recover_orphaned_running_jobs(job_store)
    await drive_job(job.job_id, job_store)
    return await run_to_terminal(job_store, job.job_id)


async def run_seeded_scripted_job(
    job_store,
    root_url: str,
    policy: PolicyLike,
    execute,
    on_seeded: Callable[[Any, Any], None] | None = None,
    **seed: Any,
):
    """Seed a row the way a crash left it, then drive it to terminal.

    Recovery tests vary three things -- what the engine answers, what the row
    already held, and how it ends -- and the seed/run/load trio spelling them
    out was one of the widest repeated lines in the tree. The engine stays a
    caller-owned name so the attempt count remains reachable after the run.

    ``on_seeded`` gets ``(job, job_store)`` after the row is persisted and
    before the first engine call, for a row shape the write path refuses to
    produce -- a corrupt policy column, a legacy envelope. It belongs here
    rather than at the call site because a test that has to mutate the row
    between the seed and the run otherwise has to hold the two halves of this
    funnel itself, and then the seeding contract is theirs to keep in sync.
    """
    return await run_seeded_job(
        _seed_job(job_store, root_url, policy, on_seeded=on_seeded, **seed), job_store, execute
    )


def recording_execute(execute, captured: list[ProducerEnvelope]):
    """Wrap a scripted engine, recording every payload dict it was handed.

    The engine call is the only place the runner's assembled batch is visible
    before results come back, so the request profile and the admitted URLs are
    read off the payloads rather than off the request the test sent.
    """

    async def _execute(payload_dict, *, settings, shutdown_event):
        captured.append(payload_dict)
        return await execute(payload_dict, settings=settings, shutdown_event=shutdown_event)

    return _execute


def job_limits(max_depth: int = 2, max_pages: int = 0, **extra) -> PolicyLike:
    """A whole-site job policy carrying nothing but its limits.

    The runner tests all state the same shape -- a policy that is a ``limits``
    block and nothing else -- and restating it inline is how two of them ended
    up capping pages at different depths while reading identically. Unset limits
    are filled in by :func:`_execution_policy` on the way to the API.
    """
    limits: dict[str, Any] = {"max_depth": max_depth, "max_pages": max_pages}
    limits.update(extra)
    return {"limits": limits}


def assert_settled(
    job,
    *,
    visited: Collection[str],
    discovered: int | None = None,
    crawled: int | None = None,
    in_flight: Collection[str] = (),
) -> None:
    """A job that ran to terminal: stopped, nothing queued, these pages kept.

    The settled-job assertion is the same claim in every whole-site job test --
    the runner drained the frontier, committed nothing in flight, and ended on
    exactly the pages that survived admission. Spelled out per test, the one
    line that matters (the frontier drained) was the one most likely to be
    forgotten, and a job that stopped with links still queued passed.

    ``visited`` is compared as a set because the pages are what the test is
    about, not the order the runner happened to commit them in; the stats a
    test does not claim are not asserted here.

    ``Collection``, not ``Container``: the comparison below builds a set from
    these, and ``Container`` promises only ``in``. A test passing a list was
    right; the annotation was the only thing saying otherwise.
    """
    assert job.status == JobStatus.COMPLETED, f"job ended {job.status}"
    assert not job.frontier, f"frontier left queued: {job.frontier}"
    admitted_in_flight = {canonical(url) for url in in_flight}
    admitted_visited = {canonical(url) for url in visited}
    assert {entry.url for entry in job.in_flight} == admitted_in_flight, (
        f"in-flight left uncommitted: {job.in_flight}"
    )
    assert set(job.visited) == admitted_visited, (
        f"visited {set(job.visited) ^ admitted_visited} differs from what the job kept"
    )
    if discovered is not None:
        assert job.stats["total_discovered"] == discovered, f"discovered {job.stats['total_discovered']}"
    if crawled is not None:
        assert job.stats["total_crawled"] == crawled, f"crawled {job.stats['total_crawled']}"


@contextmanager
def raw_job_row(job_store):
    """Direct SQL against a job's own row, for shapes the write path refuses.

    A row that only a crash or a legacy migration could leave behind cannot be
    written through the validated API, so these tests go to the table. The
    transaction lives here rather than at each call site because a raw write
    that forgets to commit is a test that passes without the row ever changing.
    """
    connection = sqlite3.connect(job_store.db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


async def submit_blocked_job(client, job_store, root_url: str, results, policy=None):
    """Submit a job whose engine blocks; return ``(job_id, release)``.

    A control-plane test has to flip a control bit while the runner is in
    flight, and without a block the job may already be terminal by the time the
    test looks. ``release`` is the event to set when the test means the
    in-flight batch to be answered.
    """
    from tests._job_harnesses import ScriptedExecute, _wait_status

    hang = asyncio.Event()
    job_id = await _create_api_job(client, root_url, ScriptedExecute(results=results, hang=hang), policy)
    await _wait_status(job_store, job_id, {"running"})
    return job_id, hang


async def post_api_job(client, root_url: str, policy: PolicyLike | None = None):
    from tests._job_harnesses import _execution_policy

    return await client.post("/v1/jobs", json={"root_url": root_url, "policy": _execution_policy(policy)})


async def post_render_with_pool(client, pool, url: str):
    from app.transport.browser_pool import set_browser_pool

    old = set_browser_pool(pool)
    try:
        return await client.post("/v1/render", json={"url": url})
    finally:
        set_browser_pool(old)


def write_row_policy(job_store, job_id: str, policy_json: str) -> None:
    """Put a policy on a job's row as raw text, bypassing the validating write path.

    A row the API would refuse -- a JSON array where a mapping belongs, a limits
    block that is a string -- can only be produced by writing the table directly,
    which is what the ``load``-path sanitizer exists to survive. The UPDATE and
    its commit live here so a test cannot write the row and forget to commit it,
    which reads as a passing sanitizer test on a row that never changed.
    """
    with raw_job_row(job_store) as connection:
        connection.execute(
            "UPDATE crawl_jobs SET policy = ? WHERE job_id = ?",
            (policy_json, job_id),
        )
