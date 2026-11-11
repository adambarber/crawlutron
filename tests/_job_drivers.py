import asyncio
import sqlite3
from collections.abc import Callable, Collection
from contextlib import contextmanager
from typing import TypedDict, Unpack

from crawlutron.admission.policy import PolicyLike
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.json_value import JsonValue
from crawlutron.storage.job_store import CrawlJobState, FrontierEntry, JobStatus, JobStore
from tests._job_store_doubles import seed_frontier
from tests._policy_factories import canonical


async def _create_api_job(client, root_url: str, execute, policy: PolicyLike | None = None):
    from crawlutron.service import job_runner

    job_runner.install_execute_crawl(execute)
    response = await post_api_job(client, root_url, policy)
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


class _SeedJobKwargs(TypedDict, total=False):
    """The seed keywords a recovery test varies."""

    frontier: list[str]
    in_flight: list[str]
    running: bool


def _seed_job(
    job_store,
    root_url: str,
    policy: PolicyLike | None = None,
    *,
    on_seeded: Callable[[object, object], None] | None = None,
    **kw: Unpack[_SeedJobKwargs],
):
    job = job_store.create(root_url, policy if policy is not None else {})
    if kw.get("frontier") is not None:
        seed_frontier(job, [FrontierEntry(url=canonical(url), depth=0) for url in kw["frontier"]])
    if kw.get("in_flight") is not None:
        job.in_flight = [FrontierEntry(url=canonical(url), depth=0) for url in kw["in_flight"]]
    job_store.save(job)
    if kw.get("running"):
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    if on_seeded is not None:
        on_seeded(job, job_store)
    return job


async def run_to_terminal(job_store, job_id: str) -> CrawlJobState:
    """Block until ``job_id`` settles; return the finished row.

    The row is re-read after the wait: a status CAS and the batch body are
    separate writes, and a stale row would be a lie the caller cannot detect."""
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

    Constructing the scripted engine is the decision; routing through here means
    a change to it cannot land in one job test and miss the other eleven."""
    from tests._job_harnesses import ScriptedExecute

    return await run_api_job(client, job_store, root_url, ScriptedExecute(results=results), policy)


def drive_job(job_id: str, job_store: JobStore, settings=None, shutdown_event=None) -> asyncio.Task[None]:
    """Run ``run_job`` for one already-persisted job; return the task.

    ``run_job``'s call shape is every runner-lifecycle test's ending, so it is
    spelled once. The task is returned, not awaited: cancel/wait tests hold it."""
    from crawlutron.config.settings import CrawlerSettings
    from crawlutron.service import job_runner

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
    from crawlutron.service import job_runner

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
    on_seeded: Callable[[object, object], None] | None = None,
    **kw: Unpack[_SeedJobKwargs],
):
    """Seed a row the way a crash left it, then drive it to terminal.

    Recovery tests vary engine answer, row shape and ending -- one of the
    widest repeated lines; ``on_seeded`` mutates the row write-path refuses."""
    return await run_seeded_job(
        _seed_job(job_store, root_url, policy, on_seeded=on_seeded, **kw), job_store, execute
    )


def recording_execute(execute, captured: list[CrawlRequest]):
    """Wrap a scripted engine, recording every request model it was handed.

    The engine call is where the runner's assembled batch is visible, so the
    profile and admitted URLs are read off the requests, not the sent API body."""

    async def _execute(payload: CrawlRequest, *, settings, shutdown_event):
        captured.append(payload)
        return await execute(payload, settings=settings, shutdown_event=shutdown_event)

    return _execute


def job_limits(max_depth: int = 2, max_pages: int = 0, **extra: JsonValue) -> PolicyLike:
    """A whole-site job policy carrying nothing but its limits.

    Restated inline, two runner tests capped pages at different depths while
    reading identically; unset limits are filled in by ``_execution_policy``."""
    limits: dict[str, JsonValue] = {"max_depth": max_depth, "max_pages": max_pages}
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
    """A job that ran to terminal: stopped, nothing queued, these pages kept -- the
    same claim in every whole-site job test, where the drained-frontier line was the
    one most likely dropped. ``visited`` is a set; ``Collection``, not ``Container``.
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

    A crash- or migration-only row cannot go through the validated API, and the
    commit lives here so a raw write cannot pass without the row changing."""
    connection = sqlite3.connect(job_store.db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


async def submit_blocked_job(client, job_store, root_url: str, results, policy=None):
    """Submit a job whose engine blocks; return ``(job_id, release)``.

    A control-plane test flips a bit while the runner is in flight; without a
    block the job may be terminal first. ``release`` answers the in-flight batch."""
    from tests._job_harnesses import ScriptedExecute, _wait_status

    hang = asyncio.Event()
    job_id = await _create_api_job(client, root_url, ScriptedExecute(results=results, hang=hang), policy)
    await _wait_status(job_store, job_id, {"running"})
    return job_id, hang


async def post_api_job(client, root_url: str, policy: PolicyLike | None = None):
    from tests._job_harnesses import _execution_policy

    return await client.post("/v1/jobs", json={"root_url": root_url, "policy": _execution_policy(policy)})


async def post_render_with_pool(client, pool, url: str):
    from crawlutron.transport.browser_pool import set_browser_pool

    old = set_browser_pool(pool)
    try:
        # Worker-behavior harness: the URL is unreachable by design (force the
        # worker path), so robots must be off or fail-closed admission skips it.
        return await client.post(
            "/v1/render",
            json={"url": url, "options": {"respect_robots": False}},
        )
    finally:
        set_browser_pool(old)


def write_row_policy(job_store, job_id: str, policy_json: str) -> None:
    """Put a policy on a job's row as raw text, bypassing the validating write path.

    A row the API would refuse is writeable only directly -- the ``load``-path
    sanitizer exists to survive it; the commit lives here so the row changes."""
    with raw_job_row(job_store) as connection:
        connection.execute(
            "UPDATE crawl_jobs SET policy = ? WHERE job_id = ?",
            (policy_json, job_id),
        )
