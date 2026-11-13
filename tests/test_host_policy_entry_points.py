from __future__ import annotations

import asyncio

import pytest

from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore
from tests._host_policy_entry_table import host_policy_entry_table as _table_entries
from tests._host_policy_probes import (
    _BLOCKED_URL,
    _assert_refused,
    _blocked_records,
    _denied_policy,
    _denied_settings,
    _effects,
    _record_probe_fetch,
    monkeypatch_wire_guard,
)
from tests._policy_factories import canonical


async def _probe_crawl_endpoint(monkeypatch, tmp_path):
    import asyncio

    from crawlutron.models.crawl_request import CrawlRequest
    from crawlutron_rest.api.crawl_endpoint import crawl

    effects = _effects()
    job_store = type(
        "Store", (), {"create_single_shot": lambda self, *args: effects["persisted"].append(args)}
    )()
    await _assert_refused(
        crawl(
            CrawlRequest(url=_BLOCKED_URL),
            _denied_settings(),
            job_store,
            # A real result store, not ``object()``.
            CrawlResultStore(tmp_path / "host-policy-results.db"),
            CrawlSpillStore(tmp_path / "host-policy-spills.db"),
            asyncio.Event(),
        ),
        effects,
    )


async def _probe_crawl_executor(monkeypatch, tmp_path):
    from crawlutron.service import crawl_executor

    effects = _effects()

    async def denied_crawl_request(payload, **kwargs):
        _record_probe_fetch(effects, payload)
        return {"results": [{"status": "blocked", "classification": "blocked"}]}

    monkeypatch.setattr(crawl_executor, "crawl_request", denied_crawl_request)
    from crawlutron.models.crawl_request import CrawlRequest

    await _assert_refused(
        crawl_executor.execute_crawl(
            CrawlRequest(url=_BLOCKED_URL),
            settings=_denied_settings(),
            # A real event: the guard refuses before it reads this, and the parameter is required.
            shutdown_event=asyncio.Event(),
        ),
        effects,
    )


async def _probe_jobs_router(monkeypatch, tmp_path):
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.models.job_create_request import JobCreateRequest
    from crawlutron_rest.api import jobs_router
    from crawlutron_rest.api.caller_auth import Tracking

    effects = _effects()
    job_store = type(
        "Store",
        (),
        {
            "create": lambda self, *args: effects["persisted"].append(args),
            "load": lambda self, *args: None,
        },
    )()
    monkeypatch.setattr(jobs_router.job_runner, "start_job", lambda *args: effects["started"].append(args))
    await _assert_refused(
        jobs_router.create_job(
            JobCreateRequest(root_url=_BLOCKED_URL),
            job_store=job_store,
            settings=_denied_settings(),
            # A real event: the handler refuses before it reads this, and the parameter is a required
            # ``asyncio.Event``.
            shutdown_event=asyncio.Event(),
            caller=CallerIdentity(),
            # The handler refuses before admission reads the tracker; the door still
            # resolves it, because the core's signature refuses an absent one.
            tracking=Tracking(job_store=job_store),
        ),
        effects,
    )


async def _probe_map_endpoint(monkeypatch, tmp_path):
    from crawlutron.models.map_request import MapRequest
    from crawlutron.service import map_endpoint

    effects = _effects()

    async def denied_execute(payload, **kwargs):
        _record_probe_fetch(effects, payload)
        return {"results": []}

    monkeypatch.setattr(map_endpoint, "execute_crawl", denied_execute)
    await _assert_refused(
        map_endpoint.map_site(
            MapRequest(url=_BLOCKED_URL),
            settings=_denied_settings(),
            shutdown_event=None,
        ),
        effects,
    )


def _watch_probe_store(effects):
    """A recording watch-store double: acreate records, count reads zero."""
    return type(
        "Store",
        (),
        {
            "acreate": lambda self, *args, **kwargs: effects["persisted"].append(repr(args)),
            "acount_for_owner": lambda self, *args, **kwargs: 0,
        },
    )()


async def _probe_watch_router(monkeypatch, tmp_path):
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.models.watch_create_request import WatchCreateRequest
    from crawlutron_rest.api import watch_router

    effects = _effects()
    await _assert_refused(
        watch_router.create_watch(
            WatchCreateRequest(url=_BLOCKED_URL, interval_seconds=60),
            watch_store=_watch_probe_store(effects),
            settings=_denied_settings(),
            caller=CallerIdentity(),
        ),
        effects,
    )


async def _probe_watch_cores(monkeypatch, tmp_path):
    """The MCP-facing register core must consult live policy itself: the MCP
    caller has no other admission edge, so the core is the probe's subject."""
    from crawlutron.models.caller_identity import CallerIdentity
    from crawlutron.models.watch_create_request import WatchCreateRequest
    from crawlutron.service import watch_cores

    effects = _effects()
    await _assert_refused(
        watch_cores.register_watch_core(
            WatchCreateRequest(url=_BLOCKED_URL, interval_seconds=60),
            _watch_probe_store(effects),
            _denied_settings(),
            CallerIdentity(),
            webhook_url=None,
            interval_floor=60,
        ),
        effects,
    )


async def _probe_site_map(monkeypatch, tmp_path):
    """A denied host must not reach the output where policy is the only filter: this
    entry point filters instead of raising, so the claim is the output, and the shape
    with no normalized root is where aggregation consults policy.
    """
    from safe_url.host_policy import HostPolicy

    from crawlutron.models.result_record import ProducerEnvelope
    from crawlutron.service.site_map import build_site_map
    from tests._result_builders import engine_result

    allowed, denied = "https://allowed.test/feed", "https://denied.test/sitemap"
    result = engine_result(
        "https://denied.test/page",
        "site_error",
        status="failed",
        normalized_url=None,
        final_url=None,
        diagnostics={"discovery": {"sitemap_urls": [], "feeds": [allowed, denied]}},
    )
    envelope: ProducerEnvelope = {
        "request_id": "req-1",
        "submitted_count": 1,
        "policy_schema_version": "1",
        "results": [result],
    }
    mapped = build_site_map(
        envelope,
        host_policy=HostPolicy.from_strings("allowed.test", "denied.test"),
    )
    assert mapped["url"] is None, mapped
    assert allowed in mapped["all_urls"], mapped
    assert denied not in mapped["all_urls"], mapped


async def _probe_render(monkeypatch, tmp_path):
    from crawlutron.service.render import render_request

    effects = _effects()

    async def worker(job):
        effects["worker"].append(job.url.raw)
        raise AssertionError("blocked render reached the worker")

    from crawlutron.models.render_request import RenderRequest

    await _assert_refused(
        render_request(
            RenderRequest.model_validate({"url": _BLOCKED_URL}),
            settings=_denied_settings(),
            worker=worker,
        ),
        effects,
    )


async def _probe_response_shaping(monkeypatch, tmp_path):
    from crawlutron.service.response_shaping import frontier_link_url

    async def invoke():
        return frontier_link_url({"normalized_url": _BLOCKED_URL}, policy=_denied_policy())

    await _assert_refused(invoke(), {"wire": []})


async def _probe_webhook(monkeypatch, tmp_path):
    from crawlutron.models.webhook_target import WebhookTarget

    effects = _effects()
    monkeypatch_wire_guard(monkeypatch, effects)
    # The admission seam IS the target's constructor: a denied host never builds
    # a target, so the delivery entries have nothing unsafe to accept.
    with pytest.raises(ValueError, match="host_denied"):
        WebhookTarget.admit(_BLOCKED_URL, host_policy=_denied_policy())
    assert effects["wire"] == []


async def _probe_webhook_delivery(monkeypatch, tmp_path):
    from crawlutron.service import webhooks

    effects = _effects()
    monkeypatch_wire_guard(monkeypatch, effects)
    await _assert_refused(
        webhooks.deliver_pinned_webhook(
            _BLOCKED_URL,
            b"{}",
            host_policy=_denied_policy(),
        ),
        effects,
    )


async def _probe_suggestions(monkeypatch, tmp_path):
    """A host this deployment refuses must produce no suggestion at either level: the
    rollup admits every stored URL through the deployment's policy, so a candidate rule
    about a refused host is not reviewable and surfaces as an empty proposal, not a raise.
    """
    from crawlutron.models.trace_response import TraceResponse
    from crawlutron.service import policy_suggestions as module

    async def _payloads_page(*, limit, cursor=None, owner=None):
        if cursor is not None:
            return []
        return [((0.0, record["request_id"]), record) for record in _blocked_records()][:limit]

    async def _retained_count(*, owner=None):
        return len(_blocked_records())

    store = type(
        "Store",
        (),
        {"apayloads_page": staticmethod(_payloads_page), "aretained_count": staticmethod(_retained_count)},
    )()

    from_handler = await module.policy_suggestions(result_store=store, settings=_denied_settings())
    assert from_handler.suggestions == []

    record = TraceResponse.model_validate(_blocked_records()[0])
    from_builder = module.build_policy_suggestions([record], host_policy=_denied_policy())
    assert from_builder.suggestions == []


async def _probe_job_runner(monkeypatch, tmp_path):
    import asyncio

    from crawlutron.service import job_runner
    from crawlutron.storage.job_store_collections import FrontierEntry, JobStatus
    from crawlutron.storage.job_store_state import CrawlJobState
    from tests._job_store_doubles import make_job_store

    effects = _effects()
    job_store = make_job_store(str(tmp_path))

    async def fetch_and_decode(url, *args, **kwargs):
        effects["fetched"].append(str(url))
        raise AssertionError("a denied job must never fetch")

    job_store.save(
        CrawlJobState(
            job_id="blocked-job",
            root_url=_BLOCKED_URL,
            policy=_denied_policy(),
            status=JobStatus.PENDING,
            frontier=[FrontierEntry(url=canonical(_BLOCKED_URL), depth=0)],
        )
    )
    await _assert_refused(
        job_runner.run_job("blocked-job", job_store, _denied_settings(), asyncio.Event(), fetch_and_decode),
        effects,
    )


async def _probe_admit_endpoint(monkeypatch, tmp_path):
    from crawlutron.models.admit_request import AdmitRequest
    from crawlutron.service.admit_endpoint import admit_url

    effects = _effects()
    monkeypatch_wire_guard(monkeypatch, effects)
    response = await admit_url(AdmitRequest(url=_BLOCKED_URL), settings=_denied_settings())
    assert response.admitted is False
    assert response.classification == "host_denied"
    assert all(not values for values in effects.values())


_PROBES = {
    "packages/rest/src/crawlutron_rest/api/crawl_endpoint.py:crawl": _probe_crawl_endpoint,
    "packages/core/src/crawlutron/service/admit_endpoint.py:admit_url": _probe_admit_endpoint,
    "packages/core/src/crawlutron/service/crawl_executor.py:execute_crawl": _probe_crawl_executor,
    "packages/core/src/crawlutron/service/job_runner.py:run_job": _probe_job_runner,
    "packages/core/src/crawlutron/service/job_cores.py:create_job_core": _probe_jobs_router,
    "packages/core/src/crawlutron/service/map_endpoint.py:map_site": _probe_map_endpoint,
    "packages/core/src/crawlutron/service/site_map.py:build_site_map": _probe_site_map,
    "packages/core/src/crawlutron/service/render.py:render_request": _probe_render,
    "packages/core/src/crawlutron/service/response_shaping.py:frontier_link_url": _probe_response_shaping,
    "packages/rest/src/crawlutron_rest/api/watch_router.py:create_watch": _probe_watch_router,
    "packages/core/src/crawlutron/service/watch_cores.py:register_watch_core": _probe_watch_cores,
    "packages/core/src/crawlutron/service/policy_suggestions.py:policy_suggestions": _probe_suggestions,
    "packages/core/src/crawlutron/service/policy_suggestions.py:build_policy_suggestions": _probe_suggestions,
    "packages/core/src/crawlutron/service/webhooks.py:deliver_pinned_webhook": _probe_webhook_delivery,
}


@pytest.mark.parametrize("entry", sorted(_PROBES))
async def test_host_policy_entry_point_refuses(entry, monkeypatch, tmp_path):
    await _PROBES[entry](monkeypatch, tmp_path)


def test_host_policy_probes_cover_the_full_api_entry_table():
    api_entries = {
        name for name in _table_entries() if not name.startswith("packages/mcp/src/crawlutron_mcp/")
    }
    assert set(_PROBES) == api_entries


async def test_render_probe_fails_when_policy_enforcement_is_removed(monkeypatch, tmp_path):
    from crawlutron.admission import payload_helpers

    def allow(url, *, policy):
        from safe_url.host_policy import HostPolicy
        from safe_url.normalized_url import NormalizedUrl

        return NormalizedUrl(url, policy=HostPolicy.unrestricted())

    # The render handler's only admission edge is the shared payload gate, so the
    # mutation lands on that consumer's bound name -- render itself holds none.
    monkeypatch.setattr(payload_helpers, "NormalizedUrl", allow)
    with pytest.raises(AssertionError):
        await _probe_render(monkeypatch, tmp_path)
