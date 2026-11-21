"""The pinned browser installer must avoid GitHub's rate-limited REST API."""

from __future__ import annotations

import camoufox.__main__ as cli
from camoufox.pkgman import AvailableVersion, RepoConfig

from scripts.camoufox_offline_fetch import PIN_SHA256, PIN_TAG, PIN_URL, pinned_versions
from tests._host_assert import host_of

REPOSITORY = "https://github.com/daijro/camoufox/releases/download/"


def test_the_pin_matches_the_camoufox_release_this_venv_expects() -> None:
    pin = cli.effective_pin(cli.load_config())
    assert pin is not None
    assert pin.tag == PIN_TAG


def test_the_download_is_a_pinned_direct_release_asset() -> None:
    assert PIN_URL.startswith(REPOSITORY)
    assert PIN_URL.endswith(".zip")
    assert len(PIN_SHA256) == 64
    assert host_of(PIN_URL) != "api.github.com"


def test_pinned_versions_carry_the_digest_without_an_api_call() -> None:
    versions = pinned_versions(RepoConfig.get_default(), PIN_TAG)
    assert len(versions) == 1
    version = versions[0]
    assert isinstance(version, AvailableVersion)
    assert version.url == PIN_URL
    assert version.sha256 == PIN_SHA256
    assert version.version.full_string == "158.0.1-beta.41"
