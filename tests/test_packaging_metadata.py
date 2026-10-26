"""The pyproject license metadata is pinned to the modern PEP 639 form (#26)."""

from __future__ import annotations

import tomllib
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent


def _project_table() -> dict:
    document = tomllib.loads((SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return document["project"]


def test_license_is_an_spdx_expression_not_a_table() -> None:
    """The legacy ``{ text = ... }`` table emits a License: field; PEP 639 wants
    a License-Expression, which hatchling writes only for the string form."""
    license_field = _project_table()["license"]
    assert license_field == "MIT", f"expected an SPDX string, got {license_field!r}"


def test_no_license_classifier_accompanies_the_expression() -> None:
    """Metadata 2.4 makes License-Expression and license classifiers mutually
    exclusive; hatchling rejects a build carrying both."""
    classifiers = _project_table().get("classifiers", [])
    offenders = [c for c in classifiers if c.startswith("License ::")]
    assert offenders == []
