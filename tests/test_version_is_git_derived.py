"""The on-disk version artifact equals the git-derived version exactly.

A checkout's generated _version.py must match what regen_version writes for the
current ref, so "the image/package version is the tag" stays a verifiable fact.
"""

from __future__ import annotations

from crawlutron import __version__
from crawlutron._version import __version__ as artifact_version
from crawlutron._version import __version_tuple__ as artifact_tuple
from scripts.regen_version import _describe, _version_parts


def test_artifact_version_matches_git_describe() -> None:
    expected, expected_tuple = _version_parts(_describe())
    assert artifact_version == expected
    assert artifact_tuple == expected_tuple


def test_public_version_matches_the_artifact() -> None:
    assert __version__ == artifact_version
