"""Surface census: every workflow is governed, waivers are bounded."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module

from tests.workflows._model import CONTINUE_ON_ERROR_WAIVERS, WORKFLOWS
from tests.workflows._registry import CATALOG
from tests.workflows._rules import BARE_PYTEST_WAIVERS, PIP_INSTALL_WAIVERS

import_module("tests.workflows._decision_catalog")  # registration, not collection

WAIVER_CEILINGS = {"continue-on-error": 1, "bare-pytest": 3, "pip-install": 1}


def missing_decisions(*, stems: Iterable[str] | None = None) -> set[str]:
    wanted = {item.stem for item in WORKFLOWS} if stems is None else set(stems)
    return wanted - set(CATALOG)


def waiver_counts() -> dict[str, int]:
    return {
        "continue-on-error": len(CONTINUE_ON_ERROR_WAIVERS),
        "bare-pytest": len(BARE_PYTEST_WAIVERS),
        "pip-install": len(PIP_INSTALL_WAIVERS),
    }
