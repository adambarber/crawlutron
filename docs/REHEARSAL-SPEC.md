# REHEARSAL-SPEC — interface appendix (#1202)

Frozen contract for `scripts/release/*` entrypoints (to be created by B1) and scripts/coverage_reports.py. Downstream tasks code against these interfaces, never internals. Items marked "[to be created]" do not exist in the tree yet; this spec is their interface, not verified behavior.

## Workspace surface (derived, not hand-listed)

- Root project: `crawlutron` (static `version = "0.5.2"` at `pyproject.toml:20`; no wheel — workspace container only).
- Members from `[tool.uv.workspace].members = ["packages/*"]`: core, extract, mcp, rest, safe-url, sdk (six member wheels).
- Per-package include patterns for coverage are derived from these members (`packages/<name>/src` for each member), never hand-typed.

## scripts/release_roster.py (exists — reference contract)

CLI: `python3 scripts/release_roster.py [--released] [--primary]`
Env contract:
- `RELEASE_TAG`: legitimately empty on a tag push (`"" or None` ⇒ resolve tags at HEAD). Non-empty names a specific tag.
- `GITHUB_EVENT_NAME`, `GITHUB_REF_NAME`: used only by `--primary`.
Outputs (stdout JSON): list of `{name, selector, version, tag, path}`.
Exit codes: 0 success; non-zero on git or roster failure.
`--released`: only entries whose `<name>-v<version>` tag is released (at HEAD, or at `RELEASE_TAG` when set).
`--primary`: prints `true` if this event owns the publish side effects for the tags at HEAD, else `false`.

## scripts/release/* entrypoints [to be created by B1]

Each entrypoint below is frozen by interface only; implementation is B1's.

### Release roster emit (replaces the inline python in release.yml's `released` job)

Name: scripts/release/emit_roster.py [to be created]
argv: python3 scripts/release/emit_roster.py (no positional args)
Env contract:
- `RELEASE_TAG`: empty on tag push ⇒ emit entries tagged at HEAD; non-empty ⇒ entries for that tag.
- `GITHUB_EVENT_NAME`, `GITHUB_REF_NAME`: feed the `--primary` resolution.
- `GITHUB_OUTPUT`: written by the caller per the contract below.
Semantic validation: `RELEASE_TAG` empty is valid (not an error); an unknown tag yields an empty released set (not an error).
Exit codes: 0 success; 2 usage or env error; 3 roster or git failure.
GITHUB_OUTPUT contract (appended by the caller):
  - `packages=<JSON array>` — released roster entries
  - `root_version=<string>` — version of the `crawlutron` entry, or `""`
  - `primary=<true|false>` — result of `release_roster.py --primary`

## scripts/coverage_reports.py [to be created by B1]

Name: scripts/coverage_reports.py
argv: python3 scripts/coverage_reports.py (no positional args; all input via files/env)
Inputs:
- Combined `.coverage` data file (produced by the battery run under coverage).
- Per-package include patterns derived from `pyproject.toml` workspace members (`packages/<name>/src` for each member).
Outputs:
- Repo-wide coverage XML + HTML.
- Per-package coverage XML + HTML (one set per member, filtered by its include pattern).
- Markdown summary path (via `--format=markdown`, supported by coverage 7.17.3 on this venv).
Contract: every output path is deterministic and relative to the repo root; no hardcoded absolute paths.

## Verification notes

- `RELEASE_TAG` empty on tag push is intentional and valid, not a missing value: `release_roster.py` treats `""` as "tags at HEAD".
- `codecov.yml` exists at repo root (informational status targets, `carryforward: true`).
- No `release-please.yml` / `scorecard.yml` edits are in scope.
