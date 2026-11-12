"""Contract pins for the crawlutron_sdk facade.

The SDK is a thin HTTP facade: it must not import crawlutron enforcement,
admission, or model internals. The server remains the sole chokepoint.
"""

from __future__ import annotations

import importlib
import pathlib
import re

_CRAWLUTRON_IMPORT = re.compile(r"^\s*(import crawlutron\b|from crawlutron\b)")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SDK_ROOT = REPO_ROOT / "packages/sdk"
# The importable package sits under src/ in the uv src-layout; the member root holds the pyproject.
_SDK_SRC = SDK_ROOT / "src" / "crawlutron_sdk"


def _sdk_sources():
    return sorted(_SDK_SRC.glob("*.py"))


def test_sdk_modules_do_not_import_crawrutron():
    for module in _sdk_sources():
        for line in module.read_text().splitlines():
            assert not _CRAWLUTRON_IMPORT.match(line), (module.name, line)


def test_sdk_is_importable():
    importlib.import_module("crawlutron_sdk")


def test_crawlutron_client_exists():
    from crawlutron_sdk import CrawlutronClient, CrawlutronClientSync

    assert CrawlutronClient is not None
    assert CrawlutronClientSync is not None


def test_sdk_wheel_packages_include_sdk():
    """The SDK's OWN wheel ships the import name, not the root's (#898 retired
    the root wheel; a root-substring assert now greens coincidentally)."""
    pyproject = (SDK_ROOT / "pyproject.toml").read_text()
    assert "crawlutron_sdk" in pyproject
