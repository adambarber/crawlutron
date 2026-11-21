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
- No posture override exists: the default excessive-permissions audit and the contract tests enforce read-only

## Code owners

- `.github/workflows/rehearsal.yml` - Release owner
- `.github/workflows/release.yml` - Release owner
- `.github/CODEOWNERS` - Declares ownership for release tooling
- `.github/zizmor.yml` - Release owner

## Unrehearsed surfaces

Rehearsal carries zero write credentials; these surfaces are observed, never rehearsed.
Each row names its deferred check.

| Surface | Where | Deferred check |
|---|---|---|
| `publish-build` | `release.yml:83-119` | `contents: read` build verification; no credentials, no attestation |
| `publish` | `release.yml:121-158` | PyPI upload + PEP 740 attestation structurally verified; real check on next tagged release |
| `image` | `release.yml:182-218` | GHCR push + SBOM attestation structurally verified; real check on next tagged release |
| `sbom`, `release-artifacts` | `release.yml:220-end` | Structurally verified; real check on next tagged release |
| TestPyPI publish | `ci.yml:702-723` | One owner-approved observation on green main `34666cb4`: all seven distributions propagated (sdist + wheel); guard unchanged, a repeat push no-ops on version-exists |

The TestPyPI observation was a push-path event, not a rehearsal capability: no dispatch
input was added and rehearsal's `contents: read` posture is unchanged.

## Contract tests

The file `tests/test_release_contract.py` verifies:
1. Attach-parity: rehearsal entrypoints are pinned to the frozen spec; B1 adopts them in release.yml
2. Image job twin: release.yml:233-317 has documented unrehearsed-surface entry
3. PR-path list: tag globs match release.yml and the spec's tag vocabulary
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