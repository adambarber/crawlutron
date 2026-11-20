# REHEARSAL — Rehearsal infrastructure runbook (#1207)

Safety invariant: rehearsal carries `permissions: contents: read` ONLY — ZERO `id-token`, zero write credentials. Credentials are absent, not boolean-guarded. No tag pushes; no automated/public release dispatch; no TestPyPI dispatch.

## Overview

Rehearsal workflows mirror production release workflows with two thin adapters over ONE program. `rehearsal.yml` and `release.yml` invoke the SAME `scripts/release/*` entrypoints; the workflows own credentials and wiring, never logic. This runbook documents the rehearsal configuration for local verification.

## Available Make targets

```sh
make rehearsal        # Run rehearsal CI jobs (reads contract tests, verifies CI behavior)
make rehearsal-bites  # Run fast-path rehearsal without id-token (lint/ruff only)
```

## Safety posture (zizmor.yml)

- `cache-poisoning` rule ignores `ci.yml` and `packaging.yml` (read-only gate jobs, no publish path)
- `posture.workflows.contents: read` declares the read-only posture for all workflows

## Code owners

- `.github/workflows/rehearsal.yml` - Release owner
- `.github/workflows/release.yml` - Release owner
- `.github/CODEOWNERS` - Declares ownership for release tooling
- `.github/zizmor.yml` - Release owner

## Contract tests

The file `tests/test_release_contract.py` verifies:
1. Attach-parity: rehearsal and production attach share same code path/mode
2. Image job twin: release.yml:233-317 has documented unrehearsed-surface entry
3. PR-path list: tag globs are pinned against REHEARSAL-SPEC.md
4. Every non-publish release job has a rehearsal twin with same entrypoint, env contract, needs edges
5. No shell control flow in rehearsal run blocks
6. CODEOWNERS lists both rehearsal.yml and release.yml
7. zizmor.yml declares read-only posture

Run with:
```sh
make rehearsal
# or directly:
.venv/bin/python -m pytest tests/test_release_contract.py -v
```

## Reference

- `docs/REHEARSAL-SPEC.md` - Frozen interface specification for `scripts/release/*` entrypoints