"""Load release entrypoints as modules for behavioral tests."""

from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from types import ModuleType

from tests.chokepoint_paths import REPO_ROOT

_SCRIPTS = REPO_ROOT / "scripts" / "release"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def load_script(name: str) -> ModuleType:
    """Load a release entrypoint by file name."""
    spec = spec_from_file_location(f"_release_{name}", _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
