"""CLI knob-exclusion clause of the facade knob-parity gate.

The CLI forwards POST /v1/crawl only (audit-1: intentionally thin); this module pins that the declared
route table stays stale-free against the same OpenAPI-derived enumeration the MCP/SDK clauses consume.
"""

from __future__ import annotations

from tests.invariants._facade_tables import (
    CLI_CLIENT_ONLY_FLAGS,
    CLI_EXCLUDED_ROUTES,
    accepted_knobs,
)


def test_cli_table_covers_non_crawl_routes():
    """The CLI serves exactly POST /v1/crawl; every other route is a declared row."""
    routes = {(k.method, k.path) for k in accepted_knobs()}
    cli_routes = routes - set(CLI_EXCLUDED_ROUTES)
    assert cli_routes == {("POST", "/v1/crawl")}, f"CLI serves undeclared routes: {cli_routes}"
    for row in CLI_EXCLUDED_ROUTES:
        assert row in routes, f"CLI exclusion row {row} matches no accepted route"


def test_client_only_flags_are_declared():
    """The client-only flag set stays nonempty and name-shaped."""
    assert CLI_CLIENT_ONLY_FLAGS >= frozenset({"key", "base_url"})
    assert all("_" in flag or flag.isalpha() for flag in CLI_CLIENT_ONLY_FLAGS)
