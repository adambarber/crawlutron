"""What a whole-site job admits, and what the admission decision leaves out.

Job execution lives in ``test_jobs_execution.py``; this file is the admission
seam on its own, because it answers a different question. Every test there asks
whether the runner moves a job through its states correctly. These ask which
URLs survive the crawl at all -- the scope regex, the depth and page limits, the
same URL reached twice, a page that answers and one that does not -- and a
failure in either family is indistinguishable from outside if the two are
interleaved in one file.

The shared shape is derived rather than repeated. ``ADMITTED``,
``ADMISSION_ROOT`` and ``ADMITTED_ONLY_SCOPE`` were spelled out at three separate
sites before they were named, and a test that declared a different ``admitted``
and then asserted a different ``visited`` passed anyway, because neither number
came from anything. ``assert_pair_crawled_once`` is here for the same reason: a
dropped or duplicated URL still leaves two results behind, so neither the result
count nor the visited set catches it -- the per-URL attempt count is the edge
that does, and every recovery test here asserts it.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api import job_runner
from app.crawl.job_store import JobStatus
from app.crawl.robots_parse import parse_robots_rules
from tests._job_harnesses import (
    ScriptedExecute,
    _create_api_job,
    _wait_status,
    assert_settled,
    job_limits,
    run_api_job,
    run_scripted_api_job,
    run_seeded_scripted_job,
    write_row_policy,
)
from tests._result_builders import _admit, _canned_result, _scripted_result_map

#: The one link every admission test here expects to survive, and the root it is
#: discovered from. Named because three tests spelled the URL and three spelled
#: the settled shape that goes with it: a test that declared a different
#: ``admitted`` and asserted a different ``visited`` passed, because neither
#: number was derived from anything.
ADMITTED = "https://example.test/included"
ADMISSION_ROOT = "https://example.test/root"
ADMITTED_ONLY_SCOPE = r"^https://example\.test/included$"


def _rules_from(fetch_robots):
    """``load_robots_rules`` over a decode rule, dropping the caching and policy
    arguments the gate supplies.

    Only the rules are this test's precondition; the origin, the fetcher and the
    policy the gate binds are its own business, and restating them here would make
    the arrangement look like the production one when it is not.
    """

    async def load(origin, fetcher, timeout_seconds, *, policy, fetch_and_decode=None):
        body, status = await fetch_robots(fetcher, f"{origin}/robots.txt", {}, timeout_seconds)
        del status
        return parse_robots_rules(body or ""), [], None

    return load


def assert_pair_crawled_once(finished, engine, root: str, child: str) -> None:
    """Both pages fetched exactly once, both recorded, both results present.

    A dropped or duplicated URL still leaves two results, so neither the result
    count nor the visited set alone catches it -- the per-URL attempt count is
    the edge that does. Stated once because every recovery test in this file
    asserts the same triple.
    """
    assert engine.attempts == {root: 1, child: 1}
    assert finished.visited == {_admit(root), _admit(child)}
    assert len(finished.results) == 2


def _root_with_alias(alias_query: str) -> tuple[str, str, str, ScriptedExecute]:
    """A seeded root, one query-aliased URL of it, an unrelated child, and an engine.

    Both alias tests are about an aliased URL being *the same work item* as the
    URL it aliases, so the shape is fixed and only the alias's query string
    differs. Written out per test it could drift in a way the assertion would
    not catch -- a different child, or a seed that was not the root -- and the
    test would then pass for a reason that had nothing to do with aliasing.
    """
    root = "https://example.test/root"
    root_alias = f"{root}?{alias_query}"
    child = "https://example.test/child"
    return root, root_alias, child, ScriptedExecute(_scripted_result_map((root, child)))


class TestWholeSiteAdmissionContract:
    @staticmethod
    def _policy(max_depth: int, max_pages: int, scope_regex: str | None = None) -> dict[str, Any]:
        policy: dict[str, Any] = {
            "limits": {
                "max_depth": max_depth,
                "max_pages": max_pages,
                "respect_robots": False,
            },
            "discovery": {"robots": True, "sitemaps": False, "feeds": True},
        }
        if scope_regex is not None:
            policy["scope_regex"] = scope_regex
        return policy

    async def _admitted(self, client, job_store, root, links, policy):
        """``links`` offered,:data:`ADMITTED` the only survivor; assert the settled shape.

        The three admission tests differ in *why* the other links were refused --
        scope regex, robots, shape -- and agree on everything else. This carries
        the agreement, so a test states its refusal and nothing else.
        """
        job = await self._admission_scenario(client, job_store, root, links, policy, admitted=[ADMITTED])
        assert_settled(job, visited={root, ADMITTED}, discovered=1, crawled=2)

    async def _admission_scenario(self, client, job_store, root, links, policy, admitted=()):
        """Run one page offering ``links``, of which exactly ``admitted`` survive.

        The per-URL attempt count is the edge that makes this a contract and not
        a coincidence: a refused link that was fetched and then dropped still
        leaves a result behind, so neither the visited set nor the result count
        can tell a correct refusal from a lucky one. Between these tests only
        the links and the policy vary, so only they are arguments -- and the
        count and the discovery total come from the argument rather than being
        restated, which is what kept two of them drifting.

        The visited set is asserted here too, for the same reason: it is the same
        statement every one of these tests ends on, derived from ``admitted``,
        and a test that declared the survivors and then asserted a different
        visited set would have passed.
        """
        engine = ScriptedExecute({root: [_canned_result(root, links=links)]})
        job = await run_api_job(client, job_store, root, engine, policy)
        assert engine.attempts == {root: 1, **{url: 1 for url in admitted}}
        assert job.stats["total_discovered"] == len(admitted)
        assert job.visited == {_admit(root), *(_admit(u) for u in admitted)}
        return job

    async def test_depth_zero_does_not_expand(self, client, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        job = await self._admission_scenario(client, job_store, root, [child], self._policy(0, 10))

        assert_settled(job, visited={"https://example.test/root"})

    async def test_depth_one_expands_exactly_one_level(self, client, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        grandchild = "https://example.test/grandchild"
        engine = ScriptedExecute(
            {
                root: [_canned_result(root, links=[child])],
                child: [_canned_result(child, links=[grandchild])],
            }
        )
        job = await run_api_job(client, job_store, root, engine, self._policy(1, 10))
        assert engine.attempts == {root: 1, child: 1}
        assert_settled(job, visited={root, child}, discovered=1)

    async def test_scope_regex_excludes_link_before_page_slot(self, client, job_store):
        await self._admitted(
            client,
            job_store,
            ADMISSION_ROOT,
            ["https://example.test/excluded", ADMITTED],
            self._policy(1, 2, ADMITTED_ONLY_SCOPE),
        )

    async def test_robots_disallowed_link_does_not_consume_page_slot(self, client, job_store, monkeypatch):
        async def fetch_robots(fetcher, url, headers, timeout_seconds):
            return "User-agent: *\nDisallow: /blocked\n", 200

        # The public loader, not the private decode rule: this test drives the
        # whole job through the API, so the gate is built inside
        # JobAdmissionContext.create and the decode rule is not a thing the test
        # holds. Patching the loader says the same thing about the same gate and
        # names something the module actually publishes.
        monkeypatch.setattr("app.crawl.admission.load_robots_rules", _rules_from(fetch_robots))
        policy = self._policy(1, 2)
        policy["limits"]["respect_robots"] = True
        await self._admitted(
            client, job_store, ADMISSION_ROOT, ["https://example.test/blocked", ADMITTED], policy
        )

    async def test_legacy_oversized_frontier_truncates_at_pop(self, job_store):
        root = "https://example.test/root"
        second = "https://example.test/second"
        third = "https://example.test/third"
        engine = ScriptedExecute(_scripted_result_map((root, second, third)))
        finished = await run_seeded_scripted_job(
            job_store, root, self._policy(1, 2), engine, frontier=[root, second, third]
        )
        assert engine.attempts == {root: 1, second: 1}
        assert finished.visited == {_admit("https://example.test/root"), _admit("https://example.test/second")}
        assert finished.frontier == []
        assert finished.stats["cap_truncated"] == 1

    async def test_in_flight_recovery_keeps_every_admitted_url(self, job_store):
        first = "https://example.test/first"
        second = "https://example.test/second"
        engine = ScriptedExecute(_scripted_result_map((first, second)))
        finished = await run_seeded_scripted_job(
            job_store,
            first,
            self._policy(1, 1),
            engine,
            frontier=[],
            in_flight=[first, second],
            running=True,
        )
        assert engine.attempts == {first: 1, second: 1}
        assert finished.visited == {_admit("https://example.test/first"), _admit("https://example.test/second")}
        assert finished.stats["cap_overcommitted"] == 1

    async def test_canonical_root_alias_counts_once(self, client, job_store):
        root = "https://example.test"
        self_alias = "https://example.test/"
        job = await self._admission_scenario(client, job_store, root, [self_alias], self._policy(1, 2))

        assert job.visited == {_admit(root)}
        assert len(job.results) == 1

    async def test_cap_truncation_is_exposed_by_job_api(self, client, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        job_id = await _create_api_job(
            client,
            root,
            ScriptedExecute(results=[_canned_result(root, links=[child])]),
            self._policy(1, 1),
        )
        assert await _wait_status(job_store, job_id, {"completed", "failed"}) == "completed"

        response = await client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        assert response.json()["cap_truncated"] is True
        assert response.json()["stats"]["cap_truncated"] == 1

    @pytest.mark.parametrize("policy_json", ['["corrupt"]', '{"limits":"corrupt"}'])
    async def test_non_dict_policy_shapes_do_not_raise_mid_crawl(self, job_store, policy_json):

        def _corrupt_policy(job, store):
            write_row_policy(store, job.job_id, policy_json)

        finished = await run_seeded_scripted_job(
            job_store,
            "https://example.test/root",
            {},
            ScriptedExecute(results=[_canned_result("https://example.test/root")]),
            on_seeded=_corrupt_policy,
        )
        assert finished.status == JobStatus.COMPLETED
        assert finished.visited == {_admit("https://example.test/root")}

    async def test_job_seeds_sitemap_pages_through_shared_admission(self, client, job_store, monkeypatch):
        root = "https://example.test/root"
        seed = "https://example.test/from-sitemap"
        policy = self._policy(1, 10)
        policy["discovery"]["sitemaps"] = True

        async def sitemap_candidates(root_url, context):
            assert root_url == root
            return [seed]

        monkeypatch.setattr(job_runner, "fetch_job_sitemap_candidates", sitemap_candidates)
        engine = ScriptedExecute({root: [_canned_result(root)], seed: [_canned_result(seed)]})
        job = await run_api_job(
            client,
            job_store,
            root,
            engine,
            policy,
        )

        assert engine.attempts == {root: 1, seed: 1}
        assert_settled(job, visited={root, seed}, discovered=1)

    async def test_analytics_and_document_links_do_not_consume_slots(self, client, job_store):
        await self._admitted(
            client,
            job_store,
            ADMISSION_ROOT,
            ["https://www.google-analytics.com/analytics.js", "https://example.test/report.pdf", ADMITTED],
            self._policy(1, 2),
        )

    async def test_robots_crawl_delay_is_committed_before_admission(self, client, job_store, monkeypatch):
        from app.scheduler import DomainScheduler

        root = "https://example.test/root"
        child = "https://example.test/child"
        committed: list[tuple[str, float]] = []

        async def allows(self, url: str) -> bool:
            return True

        async def crawl_delay(self, url: str) -> float:
            return 3.0

        def set_domain_min_delay(self, domain: str, seconds: float) -> None:
            committed.append((domain, seconds))

        monkeypatch.setattr("app.crawl.admission.RobotsGate.allows", allows)
        monkeypatch.setattr("app.crawl.admission.RobotsGate.crawl_delay", crawl_delay)
        monkeypatch.setattr(DomainScheduler, "set_domain_min_delay", set_domain_min_delay)
        policy = self._policy(1, 2)
        policy["limits"]["respect_robots"] = True
        job = await run_api_job(
            client,
            job_store,
            root,
            ScriptedExecute(results=[_canned_result(root, links=[child])]),
            policy,
        )

        assert committed == [("example.test", 3.0)]
        assert job.visited == {_admit(root), _admit(child)}

    async def test_in_flight_batch_discovering_each_other_does_not_double_crawl(self, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        engine = ScriptedExecute(_scripted_result_map((root, child), links={root: [child]}))
        finished = await run_seeded_scripted_job(
            job_store, root, self._policy(1, 2), engine, frontier=[root, child]
        )
        assert_pair_crawled_once(finished, engine, root, child)
        assert finished.stats["total_discovered"] == 0

    async def test_legacy_frontier_canonical_aliases_do_not_underfill_cap(self, job_store):
        root, root_alias, child, engine = _root_with_alias("utm_source=test")
        finished = await run_seeded_scripted_job(
            job_store, root, self._policy(1, 2), engine, frontier=[root, root_alias, child]
        )
        assert engine.attempts == {root: 1, child: 1}
        assert finished.visited == {_admit(root), _admit(child)}
        assert finished.stats["cap_truncated"] == 0

    async def test_chunked_in_flight_recovery_never_loses_admitted_urls(self, job_store):
        urls = [f"https://example.test/page-{index}" for index in range(10)]
        engine = ScriptedExecute(_scripted_result_map(urls))
        finished = await run_seeded_scripted_job(
            job_store, urls[0], self._policy(1, 2), engine, frontier=[], in_flight=urls, running=True
        )
        assert engine.attempts == {url: 1 for url in urls}
        assert finished.visited == {_admit(u) for u in urls}
        assert finished.in_flight == []
        assert finished.stats["cap_overcommitted"] == 1

    async def test_cross_list_alias_identity_is_single_work_item(self, job_store):
        root, root_alias, child, engine = _root_with_alias("utm_source=legacy")
        finished = await run_seeded_scripted_job(
            job_store,
            root,
            self._policy(1, 2),
            engine,
            frontier=[child, root_alias],
            in_flight=[root],
            running=True,
        )
        assert_pair_crawled_once(finished, engine, root, child)
        assert finished.in_flight == []
        assert finished.frontier == []

    async def test_unbounded_batch_chunking_does_not_report_truncation(self, client, job_store):
        root = "https://example.test/root"
        leaves = [f"https://example.test/leaf-{index}" for index in range(12)]
        job = await run_scripted_api_job(
            client,
            job_store,
            root,
            results=[_canned_result(root, links=leaves)] + [_canned_result(leaf) for leaf in leaves],
            policy=job_limits(max_depth=1, max_pages=0),
        )

        assert job.stats["cap_truncated"] == 0
        assert job.stats["total_crawled"] == 13
        assert len(job.results) == 13

    async def test_cap_truncated_is_exposed_by_list_and_control_responses(self, client, job_store):
        job = job_store.create("https://example.test/root", self._policy(1, 1))
        job.stats["cap_truncated"] = 1
        job_store.save(job)
        assert job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)

        listed = await client.get("/v1/jobs")
        assert listed.status_code == 200
        summary = next(item for item in listed.json()["jobs"] if item["job_id"] == job.job_id)
        assert summary["cap_truncated"] is True

        controlled = await client.post(
            f"/v1/jobs/{job.job_id}/control",
            json={"action": "cancel"},
        )
        assert controlled.status_code == 200
        assert controlled.json()["cap_truncated"] is True

    async def test_duplicate_discovered_aliases_count_once_in_stats(self, client, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        other = "https://example.test/other"
        job = await run_scripted_api_job(
            client,
            job_store,
            root,
            results=[
                _canned_result(root, links=[f"{child}?utm_source=one", f"{child}/", other]),
                _canned_result(child),
                _canned_result(other),
            ],
            policy=job_limits(max_depth=1, max_pages=3),
        )

        assert job.stats["total_discovered"] == 2
        assert job.visited == {_admit(root), _admit(child), _admit(other)}
        assert len(job.results) == 3
