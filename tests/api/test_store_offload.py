"""No sync store method runs on the event loop.

Job and watch endpoints stay off the loop; the guard is a real subclass."""

import asyncio

from app.api import job_runner
from app.config.settings import CrawlerSettings
from tests._job_harnesses import ScriptedExecute
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._result_builders import _canned_result
from tests.api._offload_doubles import (
    _off_loop,
)

_ROOT = "http://example.test/a"


_DEAD_ORIGIN = "http://127.0.0.1:9/"


class TestJobEndpointsStayOffTheLoop:
    """Every ``/v1/jobs`` handler used to reach a sync method directly -- create
    wrote, list read, get/control read twice, delete deleted -- all on the loop
    meant to be answering liveness probes.
    """

    async def test_the_whole_job_endpoint_surface_stays_off_the_loop(self, jobs_client, loop_guard_store):
        # The crawl is held open across every assertion. A test that only fails when the machine is busy is
        # asserting on timing, not on the off-the-loop behaviour it is named for.
        hang = asyncio.Event()
        job_runner.reset_job_runner()
        job_runner.install_execute_crawl(ScriptedExecute(results=[_canned_result(_ROOT)], hang=hang))
        try:
            created = await jobs_client.post(
                "/v1/jobs",
                json={"root_url": _ROOT, "policy": {"limits": {"max_depth": 1}}},
            )
            assert created.status_code == 200, created.text
            job_id = created.json()["job_id"]

            listed = await jobs_client.get("/v1/jobs")
            assert listed.status_code == 200
            assert job_id in {row["job_id"] for row in listed.json()["jobs"]}

            fetched = await jobs_client.get(f"/v1/jobs/{job_id}")
            assert fetched.status_code == 200, fetched.text

            controlled = await jobs_client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
            assert controlled.status_code == 200, controlled.text

            deleted = await jobs_client.delete(f"/v1/jobs/{job_id}")
            assert deleted.status_code == 200, deleted.text
            assert await _off_loop(loop_guard_store.load, job_id) is None
        finally:
            # Release the held crawl before the store fixture tears down, or the background task outlives the
            # loop and writes to a closed DB.
            hang.set()
            job_runner.reset_job_runner()


class TestWatchSurfaceStaysOffTheLoop:
    """The ``/v1/watches`` handlers and the sweep, on a store that refuses the loop.
    The sweep is the half that cannot fail loudly: it runs for the process's life,
    so a sync call parks the loop answering every in-flight request.
    """

    async def test_the_whole_watch_endpoint_surface_stays_off_the_loop(
        self, loop_guard_watch_store, watches_client
    ):
        created = await watches_client.post(
            "/v1/watches",
            json={"url": _ROOT, "interval_seconds": 60},
        )
        assert created.status_code == 201, created.text
        watch_id = created.json()["watch_id"]

        listed = await watches_client.get("/v1/watches")
        assert listed.status_code == 200, listed.text
        assert watch_id in {row["watch_id"] for row in listed.json()["watches"]}

        fetched = await watches_client.get(f"/v1/watches/{watch_id}")
        assert fetched.status_code == 200, fetched.text

        removed = await watches_client.delete(f"/v1/watches/{watch_id}")
        assert removed.status_code == 204, removed.text
        assert await _off_loop(loop_guard_watch_store.get, watch_id) is None

    async def test_a_sweep_pass_stays_off_the_loop(self, loop_guard_watch_store):
        """The pass reads the due rows and records each verdict off the loop too."""
        from app.api.watch_sweep import WatchSweeper

        watch = await _off_loop(
            loop_guard_watch_store.create,
            canonical(_DEAD_ORIGIN),
            interval_seconds=60,
            now=0.0,
        )
        sweeper = WatchSweeper(
            loop_guard_watch_store,
            host_policy=UNRESTRICTED_HOST_POLICY,
            settings=CrawlerSettings.from_mapping({}),
        )

        # A refused connection is the whole check: this test is about where the store is read and written, and
        # nothing else has to succeed for the verdict to be recorded.
        outcomes = await sweeper.sweep_once(now=60.0)

        assert outcomes, "the due watch was not checked at all"
        stored = await _off_loop(loop_guard_watch_store.get, watch.watch_id)
        assert stored.last_outcome is not None
