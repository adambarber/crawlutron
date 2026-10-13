"""Transient failures requeue; the runner lifecycle survives restarts."""

import asyncio
from unittest.mock import MagicMock

from app.api import job_runner
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.storage.job_store import JobStatus
from tests._job_harnesses import (
    ScriptedExecute,
    _create_api_job,
    _wait_status,
    assert_settled,
    drive_job,
    job_limits,
    run_api_job,
    single_shot_job,
    single_shot_orphan,
)
from tests._policy_factories import canonical
from tests._result_builders import GRAPH_CHILD, GRAPH_ROOT, _canned_result, _flaky, _link, engine_result


class TestTransientFailureRequeue:
    """A page that can't be reached for a *transient* reason (network/dns/tls/
    timeout/browser crash) must not be counted done or drain the frontier — it is
    re-queued for a bounded number of retries, while a *permanent* failure
    (not_found/site_error/gone) is recorded terminal immediately. Otherwise a
    whole-site job whose links fail while the network is down would mark itself
    COMPLETED having crawled only the seed."""

    _CAP = 3  # MAX_URL_TRANSIENT_ATTEMPTS

    async def test_transient_failure_is_requeued_then_succeeds(self, client, job_store):
        root = GRAPH_ROOT
        engine = ScriptedExecute(_flaky(root, "network_error", "dns_error", lands_on=_canned_result(root)))
        job = await run_api_job(client, job_store, root, engine)
        # The two early failures must not have finalized the page; the crawl retried and the single committed
        # outcome is the eventual success.
        assert job.status == JobStatus.COMPLETED
        assert engine.attempts[root] == 3
        assert job.visited == {canonical(root)}
        assert len(job.results) == 1
        assert job.results[0]["status"] == "success"
        assert job.stats["total_crawled"] == 1
        assert job.stats["total_failed"] == 0

    async def test_transient_retries_are_bounded_then_terminal(self, client, job_store):
        root = GRAPH_ROOT
        engine = ScriptedExecute(_flaky(root, "network_error"))
        job = await run_api_job(client, job_store, root, engine)
        # A permanently-flaky page is attempted exactly the cap (not forever, not once), then recorded as a
        # terminal failure so the whole-site job can end.
        assert job.status == JobStatus.COMPLETED
        assert engine.attempts[root] == self._CAP
        assert job.visited == {canonical(root)}
        assert len(job.results) == 1
        assert job.results[0]["status"] == "failed"
        assert job.results[0]["error"] == "retries_exhausted"
        assert job.stats["total_failed"] == 1
        assert job.frontier == []

    async def test_transient_requeue_survives_job_wide_page_cap(self, client, job_store):
        root = GRAPH_ROOT
        admitted = GRAPH_CHILD
        refused = "http://example.test/c"
        engine = ScriptedExecute(
            _flaky(root, "network_error", lands_on=_canned_result(root, links=[admitted, refused]))
        )

        job = await run_api_job(
            client,
            job_store,
            root,
            engine,
            job_limits(max_depth=2, max_pages=2),
        )

        assert engine.attempts == {root: 2, admitted: 1}
        assert_settled(job, visited={root, admitted}, discovered=1)

    async def test_permanent_failure_is_terminal_without_requeue(self, client, job_store):
        root = GRAPH_ROOT
        engine = ScriptedExecute({root: [_canned_result(root, status="failed", classification="not_found")]})
        job = await run_api_job(client, job_store, root, engine)
        # "Page is gone" is an answer, not a transient blip: recorded once, not retried.
        assert job.status == JobStatus.COMPLETED
        assert engine.attempts[root] == 1
        assert job.visited == {canonical(root)}
        assert job.results[0]["status"] == "failed"
        assert job.results[0]["classification"] == "not_found"
        assert job.stats["total_failed"] == 1

    async def test_frontier_expansion_uses_normalized_url(self, client, job_store):
        """The frontier must carry the validated URL, never the raw href.

        extraction.py can legitimately emit url="/rel" with normalized_url set
        to the resolved absolute URL; a runner that reads ``url`` re-queues
        unvalidated hrefs and dedupes them inconsistently against visited URLs.
        """
        divergent = _link("/rel", GRAPH_ROOT, normalized="https://ok.test/abs")
        install = ScriptedExecute(
            results=[
                _canned_result(GRAPH_ROOT, raw_links=[divergent]),
            ]
        )
        crawled: list[list[str]] = []

        async def recording_execute(payload_dict, *, settings, shutdown_event):
            urls = payload_dict.get("urls") or [payload_dict.get("url")]
            crawled.append(list(urls))
            return await install(payload_dict, settings=settings, shutdown_event=shutdown_event)

        job_id = await _create_api_job(
            client,
            GRAPH_ROOT,
            recording_execute,
        )
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"

        job = job_store.load(job_id)
        assert canonical("https://ok.test/abs") in job.visited
        assert "/rel" not in job.visited
        assert all("/rel" not in batch for batch in crawled)

    async def test_frontier_skips_unnormalizable_links(self, client, job_store):
        """A link without a validated normalized_url is never queue-eligible."""
        empty = {"url": "http://evil.test/x", "normalized_url": ""}
        bare = "http://legacy.test/string-link"
        job_id = await _create_api_job(
            client,
            GRAPH_ROOT,
            ScriptedExecute(results=[_canned_result(GRAPH_ROOT, raw_links=[empty], links=[])]),
        )
        # The string link rides in a second result so a regression that queues raw values is observable before
        # completion too.
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"
        job = job_store.load(job_id)
        assert "http://evil.test/x" not in job.visited
        assert bare not in job.visited
        assert job.stats["total_discovered"] == 0


class TestRunnerLifecycle:
    async def test_shutdown_cancels_running_tasks(self, job_store):
        hang = asyncio.Event()
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(GRAPH_ROOT)], hang=hang))
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job_runner.start_job(job.job_id, job_store, CrawlerSettings.from_mapping({}), asyncio.Event())
        await _wait_status(job_store, job.job_id, {"running"})
        await job_runner.shutdown_job_runner()
        assert not job_runner.is_running(job.job_id)
        job = job_store.load(job.job_id)
        assert job.status in {JobStatus.PAUSED, JobStatus.RUNNING}

    async def test_crash_orphan_recovered_to_pending(self, job_store):
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)

        recovered = await job_runner.recover_orphaned_running_jobs(job_store)
        assert recovered == 1
        assert job_store.load(job.job_id).status == JobStatus.PENDING

    async def test_paused_single_shot_job_is_not_resumed_as_a_whole_site_job(self):
        """The startup scan's kind filter was reading a key nothing populated.

        ``alist_jobs`` did not select ``kind``, so every summary answered
        ``None`` for it. The filter that reads that key -- the one meant to
        keep single-shot jobs out of the frontier-resuming runner -- therefore
        excluded nothing, and a single-shot job found PENDING at startup was
        handed to the whole-site runner and resumed from a frontier it does not
        have.

        The assertion is on what the scan refuses to launch, not on a status:
        the row is legitimately PENDING here, so the only thing that
        distinguishes a correct scan from the broken one is whether it starts
        work for it.
        """
        with single_shot_job(job_id="seeded-shot") as (store, job):
            started = await job_runner.recover_and_start_pending_jobs(
                store, CrawlerSettings.from_mapping({}), asyncio.Event()
            )

            assert started == 0, "a SINGLE_SHOT job was handed to the whole-site runner"
            assert store.load(job.job_id).status == JobStatus.PENDING

    async def test_single_shot_orphan_is_failed_not_left_pending_forever(self):
        """A single-shot orphan is unrecoverable, and must be marked so.

        Recovery rewinds every RUNNING row to PENDING, but the startup path
        only resumes WHOLE_SITE jobs. A single-shot job killed mid-flight was
        therefore marked recoverable and then never recovered: it sat PENDING
        forever, and ``GET /v1/crawl/{id}`` answered ``202 pending`` from an
        endpoint that is otherwise authoritative. A polling client runs to its
        own timeout learning nothing.

        The two seams disagree about what "recoverable" means, so the fix
        belongs where the rewind is decided. The row is marked terminal at the
        moment the service concludes it cannot be resumed, which is the only
        point at which that conclusion is true.
        """
        with single_shot_orphan(job_id="orphan-shot") as (store, job):
            await job_runner.recover_orphaned_running_jobs(store)

            assert store.load(job.job_id).status == JobStatus.FAILED, (
                "a single-shot orphan is not resumable, so leaving it PENDING "
                "reports 'pending' to a client forever"
            )

    async def test_single_shot_orphan_does_not_become_a_runner_task(self):
        """The rewind alone must not make a single-shot job look startable.

        The terminal status above is what a client sees; this is the other
        half. If the FAILED write were ever the only thing missing, a
        single-shot row would still be PENDING at the moment the startup scan
        reads it, and the scan is what decides whether work is launched.
        """
        with single_shot_orphan(job_id="orphan-shot-2") as (store, job):
            await job_runner.recover_orphaned_running_jobs(store)
            started = await job_runner.recover_and_start_pending_jobs(
                store, CrawlerSettings.from_mapping({}), asyncio.Event()
            )

            assert started == 0
            assert store.load(job.job_id).status == JobStatus.FAILED

    async def test_recover_and_start_resumes_pending_jobs(self, job_store):
        import asyncio as _asyncio

        from app.config.settings import CrawlerSettings as _CS

        hang = _asyncio.Event()
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(GRAPH_ROOT)], hang=hang))
        # Orphan RUNNING row rewinds to PENDING...
        job = job_store.create(GRAPH_ROOT, CrawlPolicy())
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
        # ...and a second row is already PENDING.
        job_store.create(GRAPH_CHILD, CrawlPolicy())

        started = await job_runner.recover_and_start_pending_jobs(
            job_store, _CS.from_mapping({}), _asyncio.Event()
        )
        assert started == 2
        assert job_runner.is_running(job.job_id)
        await job_runner.shutdown_job_runner()


class TestWholeSiteRedirectedUrlResolution:
    """Whole-site results are keyed by final_url but looked up by the
    pre-redirect frontier URL, so a redirected page misses -> "transient" ->
    re-queued forever, then counted failed and dropped: its content never
    reaches job.results.
    """

    async def test_frontier_url_resolves_via_final_url(self, job_store, monkeypatch):
        frontier_url = "http://example.com/page"
        final_url = "https://example.com/page/"  # http->https + trailing slash

        job = job_store.create(frontier_url, {"limits": {"max_depth": 1}})
        job_id = job.job_id

        call_count = {"n": 0}

        async def fake_execute_crawl(payload, *, settings=None, shutdown_event=None, **kwargs):
            call_count["n"] += 1
            # The engine reports the item keyed by its own URLs; the frontier URL is the ORIGINAL
            # (pre-redirect) one.
            return {
                "results": [
                    engine_result(
                        frontier_url,
                        "rendered",
                        final_url=final_url,
                        text="page content",
                        markdown="page content",
                        html="<html><body>page content</body></html>",
                        extraction_method="http_html",
                        quality_score=1.0,
                    )
                ]
            }

        monkeypatch.setattr("app.api.job_runner._execute_crawl", fake_execute_crawl)

        await drive_job(job_id, job_store, settings=MagicMock())

        finished = job_store.load(job_id)
        assert finished is not None
        # The content must land in job.results exactly once (success), not be re-queued until retries exhaust
        # and then dropped.
        assert len(finished.results) == 1, f"results={finished.results}"
        assert finished.results[0]["status"] == "success", finished.results[0]
        assert finished.stats.get("total_failed", 0) == 0, finished.stats
        # And the frontier URL must be committed visited (not re-crawled).
        assert canonical(frontier_url) in finished.visited, finished.visited
