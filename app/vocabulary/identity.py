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
# The service's own id, as callers and clients see it: one constant, seven
# references, so a rename that missed one would make the wire lie about the service.
SERVICE_ID: Final = "crawlutron"
ROUTER_MODULE_DOC: Final = (
    "Pure router: imports :func:`{handler}` and binds it\n"
    "to the ``{path}`` POST path. The handler implementation lives in the\n"
    "endpoint module so the router stays trivial and the handler is testable\n"
    "without FastAPI machinery."
)
# The project's canonical remote, on the wire in the user-agent below: a placeholder
# would name a repository that does not exist, so pyproject.toml is asserted to agree.
REPOSITORY_URL: Final = "https://github.com/adambarber/crawlutron"
# declared-crawler header and the synthetic User-Agent we attach to outbound requests, so
# the version is sourced from one place. Overridable via CRAWLUTRON_USER_AGENT.
DEFAULT_USER_AGENT: str = f"{SERVICE_ID}/{SERVICE_VERSION} (+{REPOSITORY_URL})"
USER_AGENT_TOKEN: str = SERVICE_ID
