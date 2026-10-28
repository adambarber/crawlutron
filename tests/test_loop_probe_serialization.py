"""Argument construction inside the offloaded boundary, off the event loop.

The heavy ARGUMENT (a full-body base64, a ``to_dict`` walk) must be built
inside the offloaded call, not handed to it from the loop (#535, #536).
"""

from __future__ import annotations

from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.orchestration.engine import ResultSpill
from app.transport.content_cache import ContentCache
from tests._loop_liveness import LoopLiveness as _LoopLiveness


class TestContentCacheStoreSerializationOffTheEventLoop:
    """``store_http_caches`` hands ``content_cache.aset`` its payload as an
    argument: the full-body base64 ``serialize_response`` must run INSIDE the
    offloaded boundary, not on the event loop while a >1 MiB body is encoded (#535)."""

    async def test_large_body_store_does_not_block_the_event_loop(self, tmp_path, monkeypatch) -> None:
        import app.transport.http_cache as hc
        from app.models.host_policy import HostPolicy
        from app.models.normalized_url import NormalizedUrl
        from app.transport.http_cache import store_http_caches
        from app.transport.http_response import FetchedResponse, HttpStrategyContext
        from tests._fetch_doubles import make_fetch

        liveness = _LoopLiveness()
        observed: list[int] = [-1]  # loop progress seen by the serializer
        real_serialize = hc.serialize_response

        def slow_serialize(resp):
            observed[0] = liveness.wait_for_progress()
            return real_serialize(resp)

        monkeypatch.setattr(hc, "serialize_response", slow_serialize)

        host_policy = HostPolicy.from_settings(CrawlerSettings.from_mapping())
        normalized = NormalizedUrl("http://example.com/", policy=host_policy)
        cache = ContentCache(str(tmp_path), default_ttl=60)
        ctx = HttpStrategyContext(
            original_url="http://example.com/",
            normalized_url=normalized,
            strategy="HTTP_DECLARED_CRAWLER",
            policy=CrawlPolicy(),
            host_policy=host_policy,
            http_fetcher=None,
            cache=None,
            content_cache=cache,
            caller_validators=None,
            robots_sitemaps=None,
            global_concurrency=None,
            started=0.0,
            ckey="k",
        )
        body = b"x" * (2 * 1024 * 1024)  # > 1 MiB: the base64 sweep is real work
        resp = FetchedResponse.of(make_fetch(headers={"etag": '"v1"'}, body=body))

        async def scenario() -> None:
            await store_http_caches(ctx, resp)

        await liveness.run_with_heartbeat(scenario, timeout=30)
        assert observed[0] >= liveness.needed, (
            f"the event loop made {observed[0]} heartbeat ticks while a >1 MiB body "
            "was base64-serialized for the content cache"
        )
        # The store really happened: the envelope on disk carries the body.
        stored = await cache.aget("k")
        assert stored is not None and stored["status_code"] == 200


class TestSpillWriteDictOffTheEventLoop:
    """``ResultSpill.write`` offloads ``json.dumps``, but the dict literal --
    ``to_dict(result)`` walking a multi-MB result -- must be built inside the
    offloaded call too (#536)."""

    async def test_large_result_spill_write_does_not_block_the_event_loop(
        self, tmp_path, monkeypatch
    ) -> None:
        import app.orchestration.engine as engine
        from app.models.crawl_strategy_attempt import CrawlStrategyAttempt
        from app.models.host_policy import HostPolicy
        from app.models.normalized_url import NormalizedUrl
        from app.models.result_factory import build_engine_result

        liveness = _LoopLiveness()
        observed: list[int] = [-1]  # loop progress seen by the to_dict walk
        real_to_dict = engine.to_dict

        def slow_to_dict(result):
            observed[0] = liveness.wait_for_progress()
            return real_to_dict(result)

        monkeypatch.setattr(engine, "to_dict", slow_to_dict)

        host_policy = HostPolicy.from_settings(CrawlerSettings.from_mapping())
        normalized = NormalizedUrl("http://example.com/", policy=host_policy)
        attempt = CrawlStrategyAttempt(
            strategy="HTTP_DECLARED_CRAWLER",
            status="success",
            classification="rendered",
            http_status=200,
            content_type="text/html",
            elapsed_ms=1,
        )
        result = build_engine_result(
            original_url="http://example.com/",
            normalized_url=normalized,
            final_url=normalized,
            attempt=attempt,
            http_status=200,
            headers={"content-type": "text/html"},
            text="y" * (2 * 1024 * 1024),
        )
        spill = ResultSpill(str(tmp_path / "probe.spill.ndjson"))
        try:

            async def scenario() -> None:
                await spill.write(0, result)

            await liveness.run_with_heartbeat(scenario, timeout=30)
        finally:
            spill.close()
        assert observed[0] >= liveness.needed, (
            f"the event loop made {observed[0]} heartbeat ticks while a multi-MB "
            "result was dict-walked for the spill line"
        )
