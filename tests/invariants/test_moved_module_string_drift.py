"""The moved-module string-drift invariant (#849).

After a module moves from ``crawlutron/api/<m>.py`` to
``crawlutron/service/<m>.py``, a string literal in ``tests/`` still naming the
api form — a monkeypatch target, a logger name, a process_state owner, a
path-keyed table entry — resolves against the narrow re-export shim, not the
moved module: a void patch or a dead logger/owner. Import statements repoint
visibly; string literals repoint silently-or-never. This invariant is the
standing gate: for every moved module, both reference forms —

- dotted ``crawlutron.api.<m>`` (runtime names: patch targets, loggers, owners)
- slash ``crawlutron/api/<m>.py`` (disk paths: law targets, table keys)

— must reach ZERO occurrences under ``tests/``, except rows in the exemption
table below. Every exemption row needs a written justification and is
ratcheted DOWN-only: when the shim drain empties a row's premise, the row
is deleted in the same commit. Unknown ``crawlutron.api.X`` strings where X is
a moved module are hard failures naming file:line; unknown X not moved and not
exempted fails too — the discoverer must classify it, never a silent third
category.

Scan mechanics: AST walk over every ``tests/**/*.py`` (Constants folded, so
adjacent-string concatenation and f-string literal fragments —
``ast.JoinedStr``/``FormattedValue`` — surface as matchable strings), plus a
tokenize COMMENT pass restricted to moved-module names (warnings-only, so the
gate stays strict without naming prose comments).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests._walk_primitives import comment_lines, string_constants
from tests.chokepoint_walks import REPO_ROOT

__all__ = ["MOVED_MODULES"]

_TESTS_ROOT = REPO_ROOT / "tests"

# Moved modules ledger (#849): every module ALREADY relocated to service/.
# A shim keeps resolving (assertion 1) until its drain deletes row + shim.
MOVED_MODULES: dict[str, str] = {
    "caller_concurrency": "leaf move (#849)",
    "meter_ledger": "leaf move (#849)",
    "key_throttle": "leaf move (#849)",
    "admit_endpoint": "leaf move (#849)",
    "unified_shaping": "leaf move (#849)",
    "response_shaping": "leaf move (#849)",
    "policy_defaults": "leaf move (#849)",
    "webhooks": "leaf move (#849)",
    "sweep_loop": "leaf move (#849)",
}

# Exempt (path_pattern, allowed_substring, justification) — ratcheted down-only.
EXEMPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "tests/invariants/test_moved_module_string_drift.py",
        "crawlutron",
        "this module's own tables, probes, and docstrings",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/admit_endpoint.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/caller_concurrency.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/key_throttle.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/meter_ledger.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/policy_defaults.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/response_shaping.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/sweep_loop.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/unified_shaping.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
    ),
    (
        "tests/invariants/_surface_ratchet.py",
        "crawlutron/api/webhooks.py",
        "the shim budget rows: the ratchet must name the shim path it budgets",
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
        "tests/api/test_policy_suggestions.py",
        "crawlutron.api.policy_suggestions",
        "policy_suggestions is SPLIT, stays api for now",
    ),
    (
        "tests/mcp/test_mcp_policy_suggestions.py",
        "crawlutron.api.policy_suggestions",
        "docstring naming the tool's api door; policy_suggestions stays api",
    ),
    (
        "tests/invariants/test_private_imports.py",
        "crawlutron.api.job_runner",
        "the private-imports gate's fixture strings; repoint lands with the move",
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
    (
        "tests/invariants/test_fetch_gate_discipline.py",
        "crawlutron/api/render_endpoint.py",
        "fetch-gate scope names render_endpoint (stays api); no action",
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
    for pattern, allowed, _justification in EXEMPTIONS:
        if pathlib.PurePosixPath(relative).match(pattern) or relative == pattern:
            if allowed in value:
                return True
    return False


def _drift_offenders() -> list[str]:
    offenders: list[str] = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
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


@pytest.mark.parametrize("name", sorted(MOVED_MODULES))
def test_moved_module_shim_still_resolves(name: str) -> None:
    import importlib

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
    source = "x = monkeypatch.setattr('crawlutron.api.webhooks.post_webhook', fake)\n"
    probe = tmp_path / "probe.py"
    probe.write_text(source, encoding="utf-8")
    tree = ast.parse(source)
    hits = [(lineno, value) for lineno, value in string_constants(tree) if "crawlutron.api.webhooks" in value]
    assert hits == [(1, "crawlutron.api.webhooks.post_webhook")], hits
