# CI Workflow Decisions (Issue #1204)

This document records the architectural decisions made in the CI workflow redesign (issue #1204) for future revisit triggers and audit trail.

## Scoping Revisit Trigger

**Trigger condition:** This scoping decision must be revisited when any of the following occurs:
- A new package is added to the uv workspace (packages/*/pyproject.toml)
- A new top-level directory with Python code is added
- The browser test surface expands beyond `packages/core/src/crawlutron/transport/browser*.py` and `tests/transport/test_browser*.py`
- The real-pbkdf2 surface expands beyond `packages/core/src/crawlutron/storage/**`
- A new gate job is added that could benefit from path filtering

**Revisit process:** When triggered, the owner must:
1. Update the `changes` job filters in `.github/workflows/ci.yml`
2. Update the conditional `if` expressions for affected jobs
3. Update the `required` job's `needs` list if a new gate is added
4. Update this document with the new scoping rationale
5. Verify the battery still runs unconditionally (NEVER scoped)

## Merge Queue Decision: NO

**Decision:** Do not enable GitHub merge queue for crawlutron.

**Rationale:**
1. **Single required check pattern:** The `required` job already aggregates all gate results via `needs`. GitHub's merge queue expects a single status check, but our architecture uses a composite `required` job that depends on 16+ individual gate jobs. Enabling merge queue would require restructuring the entire gate aggregation.

2. **Shallow checkout incompatibility:** Merge queue creates temporary merge commits that may not have full history. Our battery job uses shallow checkout (fetch-depth: 1) which is validated to work for the test suite, but merge queue's merge commits could break this assumption.

3. **Release-please ownership:** Version bumps and releases are driven by release-please (googleapis/release-please-action), which creates its own PRs and tags. Merge queue would interfere with release-please's branch/tag management.

4. **Linear history requirement:** The project uses rebase-first landing doctrine (fast-forward merges only). Merge queue creates merge commits by default, violating the project's git hygiene rules.

5. **Low contention:** With the current PR velocity, the cost of implementing merge queue (workflow restructuring, validation, maintenance) exceeds the benefit. The `cancel-in-progress: true` on PRs already handles superseded pushes efficiently.

**Revisit trigger for merge queue decision:**
- PR velocity exceeds 50 concurrent PRs requiring gate runs
- Release-please is replaced with a different release automation that is merge-queue compatible
- GitHub adds native support for composite required checks in merge queue configuration

## Path Filtering Contract

**Jobs that MAY carry an `if` condition (skipped passes BY DESIGN):**
- `real-pbkdf2` — filtered to core storage paths + tests + lockfile on PRs; unconditional on main push and release calls
- `test-browser` — gated on PRs via browser paths; UNCONDITIONAL on main push and release calls
- `testpypi-build` — only on main push; skips when version exists on TestPyPI
- `testpypi` — only on main push; depends on testpypi-build not being skipped
- `dependency-review` — runs on all PRs and main pushes (no path filter, but could be added in future)

**Jobs that MUST NOT carry an `if` condition (always run):**
- `actionlint`, `ruff`, `ruff-format`, `basedpyright`, `jscpd`, `mypy`, `lock`, `vulture`
- `battery` — NEVER scoped, always runs (commit admission gate)
- `coverage` — report-only, depends on battery/real-pbkdf2/test-browser
- `audit`, `secret-scan`, `zizmor`
- `required` — aggregate gate
- `packaging` — downstream of required

**Rationale:** The battery is the commit admission gate and must run on every commit. The lint/type gates are fast and catch cross-cutting issues. The audit/secret-scan/zizmor gates are supply-chain security and must not be skipped. The `required` aggregate uses `contains(needs.*.result,'failure')||contains(needs.*.result,'cancelled')` so skipped jobs (result: "skipped") pass by design — this is intentional for the filtered jobs above.

## Shallow Checkout Validation

**Jobs using shallow checkout (fetch-depth: 1, fetch-tags: false):**
- All gate jobs (actionlint, ruff, ruff-format, basedpyright, jscpd, mypy, lock, vulture, battery, audit, secret-scan, zizmor)
- `changes` job
- `real-pbkdf2` (filtered)
- `test-browser` (filtered on PRs)
- `testpypi-build`
- `coverage` (report-only)

**Jobs requiring full checkout (W0-justified):**
- `release.yml` jobs (tag pushes need tags for version derivation)
- `release-please.yml` (needs tags for version derivation)
- `rehearsal.yml` (tag pushes)
- `packaging-distributions` (needs tags for version derivation via hatch-vcs)

**Validation:** The battery job was proven to work with shallow checkout locally (fetch-depth: 1). The pre-warm bytecode step (compileall) ensures cold-run performance is acceptable. No gate job requires git history beyond the current commit.

## Zizmor Exception for Audit Cache

**Exception:** The `audit` job uses `enable-cache: true` for uv's package cache. This is a read-only cache scoped to the audit job and does not affect gate integrity. The owner applies this exception via zizmor configuration; the writer does not self-exempt.

**Documentation:** The exception is documented in the audit job's comment block in ci.yml and recorded here for audit trail.

## UV_PYTHON_PREFERENCE=only-system

**Change:** Set `UV_PYTHON_PREFERENCE=only-system` on the uv sync step in the prepare action. This ensures CI uses the system Python 3.14 provided by actions/setup-python, avoiding any uv-managed Python downloads that could introduce version drift. The old `python-preference` setup-uv input was removed: it was never a declared input and silently did nothing.

## Dependency Review Action

**Added:** `dependency-review` job using `actions/dependency-review-action@v5.0.1` (SHA-pinned) with `contents:read` permission. Runs on all PRs and main pushes. Configured via `.github/dependency-review-config.yml` to fail on high/critical vulnerabilities. Added to `required` job's `needs` list.