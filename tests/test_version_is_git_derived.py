"""Version parsing logic: deterministic output for each git-describe input."""

from __future__ import annotations

import re

import pytest


def _version_parts(desc: str) -> tuple[str, tuple[int | str, ...]]:
    """Derive a PEP 440 version from a ``git describe --tags --long --always`` string.

    Mirrors hatch-vcs's vcs source: ``vTAG-N-gHEX`` becomes
    ``TAG.devN+gHEX``; anything else passes through unchanged.
    """
    match = re.match(r"^v?(.*?)-(\d+)-g([0-9a-f]+)$", desc)
    if match:
        tag = f"{match.group(1)}.dev{match.group(2)}+g{match.group(3)}"
    else:
        tag = desc
    return tag, tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-]", tag))


@pytest.mark.parametrize(
    ("desc", "expected_version", "expected_tuple"),
    [
        # Well-formed annotated tag, standard case.
        ("v0.4.0rc1-127-g5e099dcc", "0.4.0rc1.dev127+g5e099dcc", (0, 4, "0rc1", "dev127+g5e099dcc")),
        # Tag without leading v.
        ("1.0.0-3-gabc1234", "1.0.0.dev3+gabc1234", (1, 0, 0, "dev3+gabc1234")),
        # Tag with a pre-release and post-release suffix.
        ("v2.0.0-beta.2-0-gdeadbeef", "2.0.0-beta.2.dev0+gdeadbeef", (2, 0, 0, "beta", 2, "dev0+gdeadbeef")),
        # Zero commits since the tag: still derives a dev0 suffix.
        ("v1.0.0-0-gaaaaaaa", "1.0.0.dev0+gaaaaaaa", (1, 0, 0, "dev0+gaaaaaaa")),
    ],
)
def test_version_parts_derives_pep440(desc, expected_version, expected_tuple):
    version, parts = _version_parts(desc)
    assert version == expected_version
    assert parts == expected_tuple


def test_version_parts_passes_through_non_describe_output():
    """Corrupt or missing describe output passes through unchanged."""
    for corrupt in ("", "not-a-version", "v1.2.3", "1.2.3-4"):
        version, parts = _version_parts(corrupt)
        assert version == corrupt
        assert parts == tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-]", corrupt))
