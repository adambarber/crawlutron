"""Single measured source of the coverage floors (#1358).

Code derives floors from the JSON artifact; the ratchet is UP only.
"""

from __future__ import annotations

import json

from tests._walk_primitives import package_roots
from tests.chokepoint_paths import REPO_ROOT

_ARTIFACT = REPO_ROOT / "coverage-floors.json"
_RAW = json.loads(_ARTIFACT.read_text(encoding="utf-8"))

OVERALL_FLOOR = float(_RAW["overall_floor"])
MEASURED_PACKAGES = {root: float(_RAW["measured"][root]) for root in package_roots()}
PACKAGE_FLOORS = {root: float(_RAW["package_floors"][root]) for root in package_roots()}
