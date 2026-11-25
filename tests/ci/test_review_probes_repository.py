"""Repository name validation for review probes."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "repo",
    [
        "../..",
        "owner/..",
        "..",
        "/",
        "a//b",
        "a/b/..",
        ".owner/repo",
        "owner/.repo",
        "owner/repo.",
        "owner./repo",
        "owner/../repo",
        "owner/repo/extra",
        "",
        "owner",
    ],
)
def test_malformed_repository_names_are_rejected(repo: str) -> None:
    from scripts.review_probes import is_valid_repository

    assert is_valid_repository(repo) is False


@pytest.mark.parametrize(
    "repo",
    [
        "owner/repo",
        "owner/repo.name",
        "owner/repo..name",
        "owner-name/repo_name",
        "A.B-C_D/repo.1",
        "adambarber/crawlutron",
    ],
)
def test_well_formed_repository_names_are_accepted(repo: str) -> None:
    from scripts.review_probes import is_valid_repository

    assert is_valid_repository(repo) is True
