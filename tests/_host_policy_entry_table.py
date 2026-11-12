"""The host-policy entry-point table, declared beside the tests that check it."""

from __future__ import annotations

__all__ = ["host_policy_entry_table"]


def host_policy_entry_table() -> set[str]:
    """Every module:symbol entry point the host policy must guard. #645: handle_crawl/_and_extract left as
    closures, and handle_map/render/search left with the split -- AST discovery keys `handler=` to the
    defining module (registry.py), so none are discoverable; the policy seams stay guarded.
    """
    return {
        "packages/core/src/crawlutron/service/admit_endpoint.py:admit_url",
        "packages/rest/src/crawlutron_rest/api/crawl_endpoint.py:crawl",
        "packages/core/src/crawlutron/service/crawl_executor.py:execute_crawl",
        "packages/core/src/crawlutron/service/job_runner.py:run_job",
        "packages/core/src/crawlutron/service/job_cores.py:create_job_core",
        "packages/core/src/crawlutron/service/map_endpoint.py:map_site",
        "packages/core/src/crawlutron/service/render.py:render_request",
        "packages/core/src/crawlutron/service/response_shaping.py:frontier_link_url",
        "packages/rest/src/crawlutron_rest/api/watch_router.py:create_watch",
        "packages/core/src/crawlutron/service/watch_cores.py:register_watch_core",
        "packages/core/src/crawlutron/service/policy_suggestions.py:policy_suggestions",
        "packages/core/src/crawlutron/service/site_map.py:build_site_map",
        "packages/core/src/crawlutron/service/policy_suggestions.py:build_policy_suggestions",
        # post_webhook* left the table: they take an admitted WebhookTarget, whose
        # constructor IS the policy seam (probed via deliver_pinned_webhook).
        "packages/core/src/crawlutron/service/webhooks.py:deliver_pinned_webhook",
    }
