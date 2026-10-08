"""The project's remote, service id and name are pinned to one reviewed spelling.

`REPOSITORY_URL` ships in the user-agent on every outbound request, and
`SERVICE_ID` is the token that answers for the service in robots.txt, the
discovery payloads, the MCP server name and the OpenAPI service name. The
package metadata in `pyproject.toml` has to carry the same remote and cannot
import the constant, so that half is a hand-maintained copy of a fact this tree
already owns -- which is why `test_homepage_is_the_repository_constant` exists.

So each check here is positive: it names the value the project ships with. That
is stronger than asserting some retired spelling is absent, because a rename
that missed a site fails the pin outright, and a pin cannot be satisfied by the
code agreeing with itself.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from app.vocabulary.identity import DEFAULT_USER_AGENT, REPOSITORY_URL, SERVICE_ID, SERVICE_VERSION

SERVICE_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = SERVICE_ROOT / "pyproject.toml"
CANONICAL_REMOTE = "https://github.com/adambarber/crawlutron"
DERIVED_URL_KEYS = ("Issues", "Changelog", "Security")


@pytest.fixture(scope="module")
def urls() -> dict[str, str]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["urls"]


def test_repository_url_is_the_canonical_remote() -> None:
    assert REPOSITORY_URL == CANONICAL_REMOTE


def test_service_id_is_the_brand() -> None:
    assert SERVICE_ID == "crawlutron"


def test_homepage_is_the_repository_constant(urls: dict[str, str]) -> None:
    assert urls["Homepage"] == CANONICAL_REMOTE


def test_declared_urls_derive_from_the_same_remote(urls: dict[str, str]) -> None:
    """Issues, Changelog and Security are offsets on the remote, not free text.

    Derived rather than free-written, so moving the host cannot leave three of
    the four behind.
    """
    for key in DERIVED_URL_KEYS:
        assert urls[key].startswith(f"{CANONICAL_REMOTE}/"), f"{key} does not derive from {CANONICAL_REMOTE}"


def test_user_agent_is_built_from_the_same_identity() -> None:
    """The wire copy is the one that matters, so it is pinned in full.

    Token and version are the same constants the discovery payloads announce, so
    a rename that hit the header but missed the discovery document fails here.
    """
    assert DEFAULT_USER_AGENT == f"{SERVICE_ID}/{SERVICE_VERSION} (+{CANONICAL_REMOTE})"


def test_openapi_title_is_the_brand() -> None:
    """The `/docs` title is the brand a user actually sees.

    Pinned positively because the rename missed it: the sweep matched hyphen
    and underscore spellings, and this one is spaced and title-cased, so it
    shipped an OpenAPI document still calling the service something else. A
    positive pin catches the next spelling without anyone having to remember
    to widen a ban list.
    """
    from app.main import app as service

    assert service.title == "Crawlutron Execution Service"


def test_agent_contract_is_reachable_under_the_documented_path() -> None:
    """README and AGENTS.md both send agents here, in backticks.

    A backticked path is a navigational instruction, and when the document
    and the tree disagree the reader finds out at the worst moment. This
    pins the path they cite, which is what makes the reference chain
    `test_live_docs_point_at_paths_that_exist` walks from the top.
    """
    contract = SERVICE_ROOT / "docs" / "references" / "crawlutron-agent-contract.md"
    assert contract.exists()
    assert contract.read_text(encoding="utf-8").startswith("# Crawlutron Agent Contract")


def test_contributing_clones_the_canonical_remote() -> None:
    """A contributor who follows the setup block verbatim should land in the
    repository the package metadata points at. It is the only copy a human
    reads, so it is pinned rather than trusted."""
    contributing = (SERVICE_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert f"{CANONICAL_REMOTE}.git" in contributing
