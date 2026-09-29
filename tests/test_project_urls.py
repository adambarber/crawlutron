"""The repository URL in pyproject must be the one the service puts on the wire.

`app.constants.REPOSITORY_URL` ships in the user-agent on every outbound request,
so a host that wants to identify or reach this crawler is reading that string.
`pyproject.toml` has to carry the same remote for the package metadata, and
pyproject cannot import the constant -- so these are two hand-maintained copies
of one fact, which is exactly the shape that rots.

The copy that used to be there was not merely stale but false: it named
`browser-crawler/browser-crawler`, which was never a real remote. Nothing caught
it, because no test compared the two, and the failure mode is invisible on its
own terms -- the URL is a string in a header, it still parses, it still gets
sent. So the guarantee here is not that the URL is spelled correctly, which no
test can establish offline, but that the two copies cannot disagree, and that
the one on the wire is derived from the constant rather than retyped.

This also covers the `HOME_URL` / `ISSUE_URL` split that `pyproject` expresses as
derived URLs: the bare remote, and the remote plus a path. A future edit that
moves the host without moving those derivations fails here.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from app.constants import DEFAULT_USER_AGENT, REPOSITORY_URL, SERVICE_ID, SERVICE_VERSION

SERVICE_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = SERVICE_ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def urls() -> dict[str, str]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["urls"]


def test_homepage_is_the_repository_constant(urls: dict[str, str]) -> None:
    assert urls["Homepage"] == REPOSITORY_URL


def test_declared_urls_are_derived_from_the_same_remote(urls: dict[str, str]) -> None:
    """Issues, Changelog and Security must all hang off the canonical remote.

    Derived rather than free-written, so a host change cannot leave three of the
    four pointing at the previous org.
    """
    for key in ("Issues", "Changelog", "Security"):
        assert urls[key].startswith(f"{REPOSITORY_URL}/"), f"{key} does not derive from {REPOSITORY_URL}"


def test_no_placeholder_remote_survives_anywhere(urls: dict[str, str]) -> None:
    """The pre-extraction org must not reappear in the package metadata.

    It is not enough for Homepage to be right; a leftover `browser-crawler/`
    base in any of the derived URLs still ships to PyPI as a broken link.
    """
    stale = "github.com/browser-crawler"
    offenders = {k: v for k, v in urls.items() if stale in v}
    assert not offenders, f"placeholder remote still present: {offenders}"


def test_user_agent_carries_the_real_remote_not_a_placeholder() -> None:
    """The wire copy is the one that matters, so it is asserted directly.

    It also pins the surrounding shape: the token and version are the same
    SERVICE_ID / SERVICE_VERSION the discovery payloads announce, so a rename
    that hit the header but missed the discovery document fails here.
    """
    assert DEFAULT_USER_AGENT == f"{SERVICE_ID}/{SERVICE_VERSION} (+{REPOSITORY_URL})"
    assert "github.com/browser-crawler" not in DEFAULT_USER_AGENT
    assert DEFAULT_USER_AGENT.endswith(")")


def test_contributing_clones_the_canonical_remote() -> None:
    """A contributor who followed the setup block verbatim should land in the
    repository the package metadata points at. The clone URL is a third copy and
    is the only one a human reads, so it is checked rather than trusted."""
    contributing = (SERVICE_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert f"{REPOSITORY_URL}.git" in contributing
    assert "github.com/browser-crawler" not in contributing
