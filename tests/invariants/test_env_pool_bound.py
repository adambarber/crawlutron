"""The pool knobs .env.example documents must satisfy the pool/admission bound (#300).

README + settings.py declare the invariant; the reference box is the 3 GiB
compose mem_limit the README quotes. Written red: 100/20 starved admission.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import app.config.settings as settings_mod
from app.config.settings import CrawlerSettings

_REFERENCE_LIMIT = 3 * 1024 * 1024 * 1024
_KNOB = re.compile(r"\bCRAWLUTRON_[A-Z0-9_]+\b")


class _FakeStats:
    total_bytes = _REFERENCE_LIMIT


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path(".env.example").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        match = _KNOB.match(stripped)
        if match:
            values[match.group(0)] = stripped.split("=", 1)[1].strip()
    return values


def test_documented_pool_values_satisfy_the_admission_bound():
    documented = _env_example_values()
    with (
        patch.object(settings_mod, "read_memory_stats", lambda: _FakeStats()),
        patch.dict("os.environ", documented),
    ):
        settings = CrawlerSettings.from_mapping()
        admission = max(
            1,
            settings_mod.max_http_concurrency_for(
                _REFERENCE_LIMIT, settings_mod.derive_browser_pool_size(_REFERENCE_LIMIT)
            ),
        )
    assert settings.http_max_connections >= admission, (
        f"pool ({settings.http_max_connections}) starves admission ({admission})"
    )
    assert settings.http_max_keepalive == max(20, min(128, settings.http_max_connections // 2)), (
        "keepalive does not track half the connection ceiling"
    )
