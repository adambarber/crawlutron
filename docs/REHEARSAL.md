# REHEARSAL — W0 owner verification, interface freeze, program decisions (#1202)

Scope: docs and evidence only. No workflow or code behavior changes. This ticket verifies owner facts, records frozen decisions and baselines, and freezes the scripts/release/* + scripts/coverage_reports.py interface appendix for downstream tasks (B1, B2, A4).

## Verified environment (first act)

- `git rev-parse --show-toplevel` → `.hardstand/issue_1202` (the assigned workspace)
- `git branch --show-current` → `work/issue_1202`
- `.venv/bin/python` → Python 3.14.10
- `pytest --co -q` collects 6263/6271 tests (8 deselected)
- `crawlutron.__file__` resolves under `.hardstand/issue_1202/packages/core/src/` after `scripts/bind_workspace_tree.py` (the shared pth is last-writer-wins; re-bind before each verification pass)
- Working tree clean except the untracked `BRIEF-1202.md` (never committed)
- `HEAD` == `main` == f9e94f93 (single-parent work tip; no rebase needed)

## Stale-path re-grounding (live tree, not the brief's memory)

- scripts/regen_version.py absent on disk (0 matches). Versions are static (`version = "0.5.2"` at `pyproject.toml:20`); `crawlutron.__version__` reads installed distribution metadata. No regen script exists; none is needed. Brief instruction recorded as stale, not a defect.
- scripts/coverage_reports.py absent. To be created by B1; this repo freezes only its interface (see `docs/REHEARSAL-SPEC.md`).
- scripts/release directory absent; only `scripts/release_roster.py` exists under `scripts/`. Entrypoints to be created by B1 are frozen in `docs/REHEARSAL-SPEC.md`, not implemented here.
- `release.yml`: located at `.github/workflows/release.yml`, not repo root.
- release-please.yml and scorecard.yml absent; both out of scope, no edits.
- `codecov.yml`: exists at repo root with informational status targets (`carryforward: true`) — the external claim "no codecov.yml" is false.
- `.github/workflows/ci.yml:59` uses `$/.github/actions/prepare` — recorded as an unverified syntax claim, not a defect.

## Frozen decisions (verbatim; no code change)

- Merge queue: NO. A6 not implemented.
- CodeQL / rehearsal PR checks: informational only, never block unrelated PRs.
- Battery: never split per package.
- TestPyPI guard: unchanged.
- No automated or public release workflow dispatch from this branch.
- `workflow_dispatch` is declared in `release.yml:28-34` for manual owner retry only — a separate, explicit owner authorization, not part of W0.
- No tag pushes from this branch.
- `release.yml` stays top-level.
- No edits to `release-please.yml` or `scorecard.yml` (out of scope).
- Rehearsal carries ZERO id-token / write credentials: credentials are absent, not boolean-guarded.

## Interface freeze

The contract appendix lives in `docs/REHEARSAL-SPEC.md`. It covers every `scripts/release/*` entrypoint to be created by B1 (name, argv, env contract including `RELEASE_TAG` legitimately empty on tag push, exit codes, `GITHUB_OUTPUT` contract) and scripts/coverage_reports.py (inputs, outputs, per-package include patterns derived from the `pyproject.toml` workspace members).

## Baselines (measured, not invented)

- coverage: 7.17.3 with C extension. `coverage report --format=markdown` EXISTS on this venv (`text` default, `markdown`, or `total`) — the reviewer's claim that no such flag exists is false here.
- Warm battery: `6263 passed in 32.83s` (full randomized `pytest -q`, my warm run on this workspace, 8 deselected). The reviewer's solo re-run read `6263 passed in 38.43s`. Count confirmed identical (6263); wall-time varies by runner and cache state, so neither figure is presented as a universal baseline.
- ci.yml coverage job: `.venv/bin/python -m pytest -q -m "not (browser or slow or real_pbkdf2)" --cov=crawlutron --cov-report=xml --cov-report=term-missing`, then codecov/codecov-action v7.1.1 (`files: coverage.xml`, `flags: battery`, `use_oidc: true`, `fail_ci_if_error: false`; `id-token: write`).
- Pre-push coverage hook: `.venv/bin/python -m pytest -q --cov=crawlutron --cov-report=`.
- Battery hook: `.venv/bin/python -m scripts.gate_fanout battery -- .venv/bin/python -m pytest -q`.

## Pre-landing smoke tests (unverified; settled by running)

Each is listed with its exact settling command. None is marked verified.

1. codecov-action v7.1.1 input schema for `report-type` / `use_oidc` — pending (offline here; inspect the action.yml / docs at v7.1.1).
2. sysmon branch support on Python 3.14 and ctrace for dynamic contexts — pending: `.venv/bin/python -c "import sys; sys.monitoring"` probe.
3. dorny/paths-filter pin + zizmor posture — pending (zizmor gate is whole-tree, zero tolerance; no suppression).
4. pip `--report` — pending.
5. pypiserver `--disable-fallback` — pending.
6. actionlint-py / shellcheck-py resolution on 3.14 — pending.
7. anchore / sbom-action + attest-sbom + codeql pins — pending.
8. TestPyPI version-exists endpoint shape — pending.
9. Shallow-battery proof — pending: `git clone --depth 1 file://<workspace> <scratch>`, then run the suite from the scratch tree with workspace venv + PYTHONPATH; quote the summary line.

Nothing above is marked verified. Downstream tasks run each smoke test and record the result.
