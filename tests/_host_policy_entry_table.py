"""The host-policy entry-point table, declared beside the tests that check it."""

from __future__ import annotations

__all__ = ["host_policy_entry_table"]


def host_policy_entry_table() -> set[str]:
    """Every module:symbol entry point the host policy must guard.

    #645: handle_crawl/_and_extract left as closures, and handle_map/render/search
    left with the split -- AST discovery keys `handler=` to the defining module
    (registry.py), so none are discoverable; the policy seams stay guarded.
    """
    return {
        "crawlutron/api/admit_endpoint.py:admit_url",
        "crawlutron/api/crawl_endpoint.py:crawl",
        "crawlutron/api/crawl_executor.py:execute_crawl",
        "crawlutron/api/job_runner.py:run_job",
        "crawlutron/api/job_cores.py:create_job_core",
        "crawlutron/api/map_endpoint.py:map_site",
        "crawlutron/api/render.py:render_request",
        "crawlutron/api/response_shaping.py:frontier_link_url",
        "crawlutron/api/watch_router.py:create_watch",
        "crawlutron/api/policy_suggestions.py:policy_suggestions",
        "crawlutron/api/site_map.py:build_site_map",
        "crawlutron/api/policy_suggestions.py:build_policy_suggestions",
        # post_webhook* left the table: they take an admitted WebhookTarget, whose
        # constructor IS the policy seam (probed via deliver_pinned_webhook).
        "crawlutron/api/webhooks.py:deliver_pinned_webhook",
    }
