"""Proof the safe-url package is standalone: importing it with only its own src
tree on PYTHONPATH (no crawlutron-core/rest/mcp/sdk) succeeds and ``__all__`` is
fully populated."""

from __future__ import annotations

import os
import subprocess
import sys

from tests._walk_primitives import REPO_ROOT

_SAFE_URL_SRC = REPO_ROOT / "packages" / "safe-url" / "src"


def test_safe_url_imports_with_only_its_src_on_path():
    """PYTHONPATH = packages/safe-url/src alone, with crawlutron roots stripped
    from sys.path: ``import safe_url`` succeeds and every name in
    ``safe_url.__all__`` resolves to a real attribute."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SAFE_URL_SRC)
    code = (
        "import sys\n"
        "# Keep only the safe-url src tree among repo ``packages/*/src`` entries so\n"
        "# crawlutron-core/rest/mcp/sdk cannot be imported by the package under test.\n"
        'sys.path = [p for p in sys.path if ("safe-url" in p) or (not p.endswith("/src"))]\n'
        "import safe_url\n"
        "missing = [name for name in safe_url.__all__ if not hasattr(safe_url, name)]\n"
        "raise SystemExit(1 if missing or not safe_url.__all__ else 0)\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout
