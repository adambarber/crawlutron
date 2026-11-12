"""The moved-module string-drift invariant (#849): the ledger is EMPTY. Consumers import from
``crawlutron.service`` directly; the detector stays as the standing tripwire for any FUTURE move --
register the module in MOVED_MODULES when its api shim lands, delete the row when the shim dies.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests._walk_primitives import comment_lines, is_exempt, string_constants
from tests.chokepoint_walks import REPO_ROOT

__all__ = ["MOVED_MODULES"]

_TESTS_ROOT = REPO_ROOT / "tests"

# Moved modules ledger (#849): drained to EMPTY by the shim drain — no api
# re-export shim remains; the drain gate holds the tree at zero, unconditionally.
MOVED_MODULES: dict[str, str] = {}

# Exempt (path_pattern, allowed_substring, justification) — ratcheted down-only.
EXEMPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "tests/invariants/test_moved_module_string_drift.py",
        "crawlutron",
        "this module's own tables, probes, and docstrings",
    ),
    (
        "tests/invariants/test_host_policy_entry_points.py",
        "crawlutron.api._undeclared_entry",
        "synthetic probe name for the gate's own bite check (not a real module)",
    ),
    (
        "tests/test_no_runtime_import_cycles.py",
        "crawlutron.api.planted_",
        "synthetic planted probe modules, not real api modules",
    ),
    (
        "tests/test_module_graph.py",
        "crawlutron.api.crawl_endpoint",
        "cycle-probe subjects; crawl_endpoint stays api",
    ),
    (
        "tests/test_module_graph.py",
        "crawlutron.api.caller_auth",
        "cycle-probe subjects; caller_auth stays api until its split",
    ),
    (
        "tests/invariants/test_layer_exempt_ratchet.py",
        "crawlutron/api/anonymous_throttle.py",
        "comment naming the module that stays api (HTTP-only ASGI middleware)",
    ),
    (
        "tests/_walk_primitives.py",
        "crawlutron/service/webhooks.py",
        "docstring example only; the sweep's own docstring may name a moved path",
    ),
    (
        "tests/api/test_anonymous_throttle.py",
        "crawlutron.api.anonymous_throttle",
        "anonymous_throttle stays api (HTTP-ONLY ASGI middleware)",
    ),
    (
        "tests/test_error_envelope.py",
        "crawlutron.api.exceptions",
        "exceptions stays api (HTTP-ONLY envelope mapping)",
    ),
    (
        "tests/invariants/test_result_errors.py",
        "crawlutron.api.exceptions",
        "exceptions stays api (HTTP-ONLY envelope mapping)",
    ),
    (
        "tests/api/test_caller_auth.py",
        "crawlutron.api.caller_auth",
        "caller_auth stays api until its split",
    ),
    (
        "tests/invariants/test_url_secrets_law.py",
        "crawlutron/api/watch_router.py",
        "watch_router stays api (HTTP-ONLY); no action",
    ),
    (
        "tests/invariants/test_url_secrets_law.py",
        "crawlutron/api/crawl_endpoint.py",
        "crawl_endpoint stays api (SPLIT, HTTP part); no action",
    ),
)


def _comment_mentions(source: str) -> list[tuple[int, str]]:
    """Comments naming a moved module (dotted or slash form) — warning surface."""
    mentions: list[tuple[int, str]] = []
    for lineno, token_text in comment_lines(source):
        for name in MOVED_MODULES:
            if f"crawlutron.api.{name}" in token_text or f"crawlutron/api/{name}.py" in token_text:
                mentions.append((lineno, token_text.strip()))
                break
    return mentions


def _exempt(relative: str, value: str) -> bool:
    return is_exempt(relative, value, EXEMPTIONS)


def _drift_offenders(root: pathlib.Path = _TESTS_ROOT) -> list[str]:
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        relative = (
            path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else path.as_posix()
        )
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for lineno, value in string_constants(tree):
            for name in MOVED_MODULES:
                dotted = f"crawlutron.api.{name}"
                slash = f"crawlutron/api/{name}.py"
                if dotted in value or slash in value:
                    if _exempt(relative, value):
                        continue
                    offenders.append(f"{relative}:{lineno} names moved module '{name}': {value[:100]!r}")
        for lineno, comment in _comment_mentions(source):
            if _exempt(relative, comment):
                continue
            offenders.append(f"{relative}:{lineno} COMMENT names a moved module: {comment[:100]!r}")
    return offenders


def test_moved_module_shim_still_resolves() -> None:
    import importlib

    for name in sorted(MOVED_MODULES):
        importlib.import_module(f"crawlutron.api.{name}")


def test_no_stale_api_strings_for_moved_modules() -> None:
    offenders = _drift_offenders()
    assert not offenders, (
        "stale crawlutron.api.* string references to moved modules "
        "(repoint to crawlutron.service.* — a string naming the shim is a void patch): "
        + "; ".join(offenders)
    )


def test_drift_detector_bites(tmp_path: pathlib.Path) -> None:
    """Mutant check: a stale dotted string must be named file:line + module."""
    MOVED_MODULES["webhooks"] = "probe row"
    try:
        source = "x = monkeypatch.setattr('crawlutron.api.webhooks.post_webhook', fake)\n"
        probe = tmp_path / "probe.py"
        probe.write_text(source, encoding="utf-8")
        tree = ast.parse(source)
        hits = [
            (lineno, value) for lineno, value in string_constants(tree) if "crawlutron.api.webhooks" in value
        ]
        assert hits == [(1, "crawlutron.api.webhooks.post_webhook")], hits
        offenders = _drift_offenders(tmp_path)
        assert any("names moved module 'webhooks'" in o for o in offenders), offenders
    finally:
        MOVED_MODULES.pop("webhooks", None)
    assert not _drift_offenders(), "the probe row leaked into the ledger"


def test_moved_module_shim_gate_bites_on_shimless_row() -> None:
    """Mutation probe: a row with no api shim reds the shim gate, naming it."""
    MOVED_MODULES["webhooks"] = "probe row"
    try:
        with pytest.raises(ImportError, match="crawlutron.api.webhooks"):
            test_moved_module_shim_still_resolves()
    finally:
        MOVED_MODULES.pop("webhooks", None)
    test_moved_module_shim_still_resolves()
    assert not _drift_offenders(), "the probe row leaked into the ledger"


def test_moved_module_shim_gate_green_when_shim_exists() -> None:
    """Positive control: a row naming an existing api module stays green."""
    MOVED_MODULES["crawl_endpoint"] = "probe row"
    try:
        test_moved_module_shim_still_resolves()
    finally:
        MOVED_MODULES.pop("crawl_endpoint", None)
