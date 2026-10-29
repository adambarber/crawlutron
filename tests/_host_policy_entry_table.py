"""The host-policy entry-point table, declared beside the tests that check it."""

from __future__ import annotations

__all__ = ["host_policy_entry_table"]


def host_policy_entry_table() -> set[str]:
    """Every module:symbol entry point the host policy must guard."""
    return {
        "app/api/crawl_endpoint.py:crawl",
        "app/api/crawl_executor.py:execute_crawl",
        "app/api/job_runner.py:run_job",
        "app/api/jobs_router.py:create_job",
        "app/api/map_endpoint.py:map_site",
        "app/api/render.py:render_request",
        "app/api/async_crawl_runner.py:run_async_crawl",
        "app/api/response_shaping.py:frontier_link_url",
        "app/api/watch_router.py:create_watch",
        "app/api/policy_suggestions.py:policy_suggestions",
        "app/api/site_map.py:build_site_map",
        "app/api/policy_suggestions.py:build_policy_suggestions",
        "app/api/webhooks.py:post_webhook",
        "app/api/webhooks.py:post_webhook_guarded",
        "app/api/webhooks.py:start_webhook_delivery",
        "app/api/webhooks.py:deliver_pinned_webhook",
        "app/mcp/tools.py:handle_crawl",
        "app/mcp/tools.py:handle_crawl_and_extract",
        "app/mcp/tools.py:handle_map",
        "app/mcp/tools.py:handle_render",
        "app/mcp/tools.py:handle_search",
    }
