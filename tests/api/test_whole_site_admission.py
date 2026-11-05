"""What a whole-site job admits, and what the admission decision leaves out.

Run-state questions live elsewhere; these ask which URLs survive. The per-URL
attempt count is the edge that catches a dropped or duplicated URL."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, NamedTuple

import pytest
from httpx import AsyncClient

from crawlutron.admission.policy import PolicyLike
from crawlutron.api import job_runner
from crawlutron.models.json_value import JsonValue
from crawlutron.storage.job_store import CrawlJobState, JobStatus, JobStore
from crawlutron.transport.robots import RobotsRules
from crawlutron.transport.robots_parse import parse_robots_rules
from crawlutron.vocabulary.robots import RobotsSource
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
from tests._policy_factories import canonical
from tests._result_builders import engine_result

#: The one link every admission test here expects to survive, and the root it is discovered from.
ADMITTED = "https://example.test/included"
ADMISSION_ROOT = "https://example.test/root"
ADMITTED_ONLY_SCOPE = r"^https://example\.test/included$"


def _rules_from(
    fetch_robots: Callable[[Any, str, dict[str, str], int], Awaitable[tuple[str, int]]],
) -> Callable[..., Awaitable[RobotsRules]]:
    """``load_robots_rules`` over a decode rule, dropping what the gate supplies.

    Only the rules are the precondition; the origin, fetcher and policy are the
    gate's own business, not the production arrangement."""

    async def load(origin, fetcher, timeout_seconds, *, policy, fetch_and_decode=None):
        body, status = await fetch_robots(fetcher, f"{origin}/robots.txt", {}, timeout_seconds)
        del status
        return RobotsRules(parse_robots_rules(body or ""), [], None, RobotsSource.policy)

    return load


def assert_pair_crawled_once(finished: CrawlJobState, engine: ScriptedExecute, root: str, child: str) -> None:
    """Both pages fetched exactly once, both recorded, both results present.

    A dropped or duplicated URL still leaves two results; only the per-URL
    attempt count catches it -- asserted once for every recovery test."""
    assert engine.attempts == {root: 1, child: 1}
    assert finished.visited == {canonical(root), canonical(child)}
    assert len(finished.results) == 2


class _AliasSite(NamedTuple):
    """The seed, its query alias, the unrelated child, and the engine.

    Both alias tests are about an aliased URL being the same work item as the
    URL it aliases; the shape is fixed, only the alias's query string differs."""

    root: str
    root_alias: str
    child: str
    engine: ScriptedExecute


def _root_with_alias(alias_query: str) -> _AliasSite:
    root = "https://example.test/root"
    root_alias = f"{root}?{alias_query}"
    child = "https://example.test/child"
    return _AliasSite(
        root,
        root_alias,
        child,
        ScriptedExecute({u: [engine_result(u, "rendered")] for u in (root, child)}),
    )


def _contract_policy(
    max_depth: int,
    max_pages: int,
    scope_regex: str | None = None,
    *,
    respect_robots=False,
    sitemaps=False,
) -> PolicyLike:
    """Site-wide policy rows; ``scope_regex`` narrows the admitted link set."""
    policy: dict[str, JsonValue] = {
        "limits": {"max_depth": max_depth, "max_pages": max_pages, "respect_robots": respect_robots},
        "discovery": {"robots": True, "sitemaps": sitemaps, "feeds": True},
    }
    if scope_regex is not None:
        policy["scope_regex"] = scope_regex
    return policy


class TestWholeSiteAdmissionContract:
    _policy = staticmethod(_contract_policy)

    async def _admitted(
        self,
        client: AsyncClient,
        job_store: JobStore,
        root: str,
        links: Sequence[str],
        policy: PolicyLike,
    ) -> None:
        """``links`` offered, :data:`ADMITTED` the only survivor; assert the settlement.

        The admission tests differ only in *why* the other links were refused
        (scope, robots, shape); this carries the agreement."""
        job = await self._admission_scenario(client, job_store, root, links, policy, admitted=[ADMITTED])
        assert_settled(job, visited={root, ADMITTED}, discovered=1, crawled=2)

    async def _admission_scenario(
        self,
        client: AsyncClient,
        job_store: JobStore,
        root: str,
        links: Sequence[str],
        policy: PolicyLike,
        admitted: Sequence[str] = (),
    ) -> CrawlJobState:
        """Run one page offering ``links``, of which exactly ``admitted`` survive.

        Only links and policy vary across tests; the attempt count, discovery
        total and visited set derive from ``admitted``, never restated."""
        engine = ScriptedExecute({root: [engine_result(root, "rendered", links=list(links))]})
        job = await run_api_job(client, job_store, root, engine, policy)
        assert engine.attempts == {root: 1, **{url: 1 for url in admitted}}
        assert job.stats["total_discovered"] == len(admitted)
        assert job.visited == {canonical(root), *(canonical(u) for u in admitted)}
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
        chain = {
            root: [engine_result(root, "rendered", links=[child])],
            child: [engine_result(child, "rendered", links=[grandchild])],
        }
        engine = ScriptedExecute(chain)
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

        # The public loader, not the private decode rule: this test drives the whole job through the API, so
        # the gate is built inside JobAdmissionContext.create and the decode rule is not a thing the test
        monkeypatch.setattr("crawlutron.admission.admission.load_robots_rules", _rules_from(fetch_robots))
        policy = self._policy(1, 2, respect_robots=True)
        await self._admitted(
            client, job_store, ADMISSION_ROOT, ["https://example.test/blocked", ADMITTED], policy
        )

    async def test_legacy_oversized_frontier_truncates_at_pop(self, job_store):
        root = "https://example.test/root"
        second = "https://example.test/second"
        third = "https://example.test/third"
        engine = ScriptedExecute({u: [engine_result(u, "rendered")] for u in (root, second, third)})
        finished = await run_seeded_scripted_job(
            job_store, root, self._policy(1, 2), engine, frontier=[root, second, third]
        )
        assert engine.attempts == {root: 1, second: 1}
        assert finished.visited == {
            canonical("https://example.test/root"),
            canonical("https://example.test/second"),
        }
        assert finished.frontier == []
        assert finished.stats["cap_truncated"] == 1

    async def test_in_flight_recovery_keeps_every_admitted_url(self, job_store):
        first = "https://example.test/first"
        second = "https://example.test/second"
        engine = ScriptedExecute({u: [engine_result(u, "rendered")] for u in (first, second)})
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
        assert finished.visited == {
            canonical("https://example.test/first"),
            canonical("https://example.test/second"),
        }
        assert finished.stats["cap_overcommitted"] == 1

    async def test_cap_truncation_is_exposed_by_job_api(self, client, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        job_id = await _create_api_job(
            client,
            root,
            ScriptedExecute(results=[engine_result(root, "rendered", links=[child])]),
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
            ScriptedExecute(results=[engine_result("https://example.test/root", "rendered")]),
            on_seeded=_corrupt_policy,
        )
        assert finished.status == JobStatus.COMPLETED
        assert finished.visited == {canonical("https://example.test/root")}

    async def test_job_seeds_sitemap_pages_through_shared_admission(self, client, job_store, monkeypatch):
        root = "https://example.test/root"
        seed = "https://example.test/from-sitemap"
        policy = self._policy(1, 10, scope_regex="^https://example.test/", sitemaps=True)

        async def sitemap_candidates(root_url, context):
            assert root_url == root
            return [seed]

        monkeypatch.setattr(job_runner, "fetch_job_sitemap_candidates", sitemap_candidates)
        engine = ScriptedExecute(
            {root: [engine_result(root, "rendered")], seed: [engine_result(seed, "rendered")]}
        )
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
        from crawlutron.transport.scheduler import DomainScheduler

        root = "https://example.test/root"
        child = "https://example.test/child"
        committed: list[tuple[str, float]] = []

        async def allows(self, url: str) -> bool:
            return True

        async def crawl_delay(self, url: str) -> float:
            return 3.0

        def set_domain_min_delay(self, domain: str, seconds: float) -> None:
            committed.append((domain, seconds))

        monkeypatch.setattr("crawlutron.admission.admission.RobotsGate.allows", allows)
        monkeypatch.setattr("crawlutron.admission.admission.RobotsGate.crawl_delay", crawl_delay)
        monkeypatch.setattr(DomainScheduler, "set_domain_min_delay", set_domain_min_delay)
        policy = self._policy(1, 2, respect_robots=True)
        job = await run_api_job(
            client,
            job_store,
            root,
            ScriptedExecute(results=[engine_result(root, "rendered", links=[child])]),
            policy,
        )

        assert committed == [("example.test", 3.0)]
        assert job.visited == {canonical(root), canonical(child)}

    async def test_in_flight_batch_discovering_each_other_does_not_double_crawl(self, job_store):
        root = "https://example.test/root"
        child = "https://example.test/child"
        # The root hands the child to the frontier while the child is still in flight.
        engine = ScriptedExecute(
            {
                root: [engine_result(root, "rendered", links=[child])],
                child: [engine_result(child, "rendered")],
            }
        )
        finished = await run_seeded_scripted_job(
            job_store, root, self._policy(1, 2), engine, frontier=[root, child]
        )
        assert_pair_crawled_once(finished, engine, root, child)
        assert finished.stats["total_discovered"] == 0

    async def test_chunked_in_flight_recovery_never_loses_admitted_urls(self, job_store):
        urls = [f"https://example.test/page-{index}" for index in range(10)]
        engine = ScriptedExecute({u: [engine_result(u, "rendered")] for u in urls})
        finished = await run_seeded_scripted_job(
            job_store, urls[0], self._policy(1, 2), engine, frontier=[], in_flight=urls, running=True
        )
        assert engine.attempts == {url: 1 for url in urls}
        assert finished.visited == {canonical(u) for u in urls}
        assert finished.in_flight == []
        assert finished.stats["cap_overcommitted"] == 1

    async def test_unbounded_batch_chunking_does_not_report_truncation(self, client, job_store):
        root = "https://example.test/root"
        leaves = [f"https://example.test/leaf-{index}" for index in range(12)]
        job = await run_scripted_api_job(
            client,
            job_store,
            root,
            results=[engine_result(root, "rendered", links=leaves)]
            + [engine_result(leaf, "rendered") for leaf in leaves],
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
                engine_result(root, "rendered", links=[f"{child}?utm_source=one", f"{child}/", other]),
                engine_result(child, "rendered"),
                engine_result(f"{child}/", "rendered"),
                engine_result(other, "rendered"),
            ],
            policy=job_limits(max_depth=1, max_pages=4),
        )
        assert job.stats["total_discovered"] == 3  # #520: the slash variant is distinct path text
        assert job.visited == {canonical(root), canonical(child), canonical(f"{child}/"), canonical(other)}
        assert len(job.results) == 4


class TestWholeSiteAliasIdentity:
    """Alias-shaped URLs (#520): the query alias is the same page, the slash
    variant is a distinct one -- the cases the cap accounting once underfilled."""

    @staticmethod
    def _policy(max_depth: int, max_pages: int) -> PolicyLike:
        return {
            "limits": {"max_depth": max_depth, "max_pages": max_pages, "respect_robots": False},
            "discovery": {"robots": True, "sitemaps": False, "feeds": True},
        }

    async def test_canonical_root_alias_counts_once(self, client, job_store):
        root = "https://example.test/root"
        self_alias = f"{root}?utm_source=tw"  # "root/" is distinct now; the query alias is not
        engine = ScriptedExecute({root: [engine_result(root, "rendered", links=[self_alias])]})
        job = await run_api_job(client, job_store, root, engine, self._policy(1, 2))

        assert job.visited == {canonical(root)}
        assert len(job.results) == 1

    async def test_legacy_frontier_canonical_aliases_do_not_underfill_cap(self, job_store):
        root, root_alias, child, engine = _root_with_alias("utm_source=test")
        finished = await run_seeded_scripted_job(
            job_store, root, self._policy(1, 2), engine, frontier=[root, root_alias, child]
        )
        assert_pair_crawled_once(finished, engine, root, child)
        assert finished.stats["cap_truncated"] == 0

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
