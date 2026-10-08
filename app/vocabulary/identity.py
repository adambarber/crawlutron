"""Who this service says it is.

The service's own id, version, repository and user-agent. These are the strings
that go out on the wire -- in the user-agent header, the MCP server name, the
OpenAPI service name and the discovery payloads -- so they are declared once and
imported everywhere; a site that spells one by hand is a site that can disagree
with the service it is talking to.
"""

from __future__ import annotations

from typing import Final

# Service version: single source of truth, imported directly by callers.
SERVICE_VERSION: str = "0.3.0"
# The service's own id, as callers and clients see it. This string was spelled
# at seven sites -- the three discovery payloads, the MCP server name, the
# OpenAPI service_name and its contact name, and the user-agent token -- and
# they all answer for the same service, so a rename that missed one would make
# the wire describe something other than what replied. One constant, seven
# references: a site that drifts is now a visible edit rather than a silent lie.
SERVICE_ID: Final = "crawlutron"
ROUTER_MODULE_DOC: Final = (
    "Pure router: imports :func:`{handler}` and binds it\n"
    "to the ``{path}`` POST path. The handler implementation lives in the\n"
    "endpoint module so the router stays trivial and the handler is testable\n"
    "without FastAPI machinery."
)
# The project's canonical remote. It goes out on the wire in the user-agent
# below, so a placeholder here is not a documentation wart: it tells every host
# the crawler visits that a repository which does not exist is where to find
# the code. pyproject.toml needs the same value and cannot import this, so
# tests/test_project_urls.py asserts the two agree rather than trusting a copy
# to stay in step.
REPOSITORY_URL: Final = "https://github.com/adambarber/crawlutron"
# Public-facing identity strings. Kept here so the same UA appears in both the
# declared-crawler header and the synthetic ``User-Agent:`` line we attach
# to outbound requests, and so the package version is sourced from a single
# place instead of being duplicated across files.
# Overridable via CRAWLUTRON_USER_AGENT.
DEFAULT_USER_AGENT: str = f"{SERVICE_ID}/{SERVICE_VERSION} (+{REPOSITORY_URL})"
USER_AGENT_TOKEN: str = SERVICE_ID
