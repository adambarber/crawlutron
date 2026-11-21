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
- `GITHUB_OUTPUT`: emit_roster.py itself appends the three contract lines — it is the caller; no separate caller is implied.
Stdout: one-line summary (`N package(s) tagged; root=...; primary=...`), never the contract lines.
Semantic validation: `RELEASE_TAG` empty is valid (not an error); an unknown tag yields an empty released set (not an error).
Exit codes: 0 success; 2 usage or env error; 3 roster or git failure.
GITHUB_OUTPUT contract (emitted by emit_roster.py itself):
  - `packages=<JSON array>` — released roster entries (same entries `release_roster.py --released` prints)
  - `root_version=<string>` — version of the `crawlutron` entry, or `""`
  - `primary=<true|false>` — result of `release_roster.py --primary`
Coherence: `release_roster.py` prints its stdout JSON array pretty (`indent=2`, scripts/release_roster.py:114); emit_roster.py reads that array and re-serializes it compactly into `GITHUB_OUTPUT packages=<JSON array>`, matching the compact form `release.yml:92-94` uses. `primary` is always re-resolved via `release_roster.py --primary`, never copied from a stale value.

### Distribution build + version assert (release.yml:101-163, `publish-build`)

Name: scripts/release/build_assert.py [to be created]
argv: python3 scripts/release/build_assert.py (no positional args)
Env contract:
- `SELECTOR`: package selector (`name` field) from the released entry, e.g. `crawlutron-core`.
- `VERSION`: package version from the released entry (`EXPECTED_VERSION`).
- Permissions required: none (no OIDC/id-token — a build backend executes arbitrary code, so a job holding id-token must not also build).
- Builds only this distribution: `uv build --package "$SELECTOR" --out-dir dist`.
- Asserts the built artifacts carry the release version: each wheel's `.dist-info/METADATA` `Version` header equals `EXPECTED_VERSION` (mismatch fails, naming the wheel and its carried version).
- Runs `uvx twine check --strict dist/*` on the verified artifacts.
Exit codes: 0 success (built, version asserted, twine check strict passes); 2 env/arg error; 3 build failure; 4 version mismatch; 5 twine check failure.
Semantic validation: no wheel built ⇒ failure; a wheel carrying a version other than the released entry's version ⇒ failure naming file+line. Static versions in `pyproject.toml` bind the build to the exact release commit; a repeat push reuses the declared version (`skip-existing` makes TestPyPI upload a no-op until release-please bumps it).
RELEASE_TAG rule: `SELECTOR`/`VERSION` come from the released entry resolved at HEAD (or at `RELEASE_TAG` when non-empty); empty `RELEASE_TAG` on tag push resolves tags at HEAD.

### Container image build, smoke, push, attest (release.yml:233-316)

Name: scripts/release/image.py [to be created]
argv: python3 scripts/release/image.py (no positional args)
Env contract:
- `NATIVE_VERSION`: root package version (release root_version); drives the image tag (lowercased, `+` → `-`).
- Permissions required: `packages: write`, `attestations: write`, `id-token: write`.
- Builds from the workflow's fixed Dockerfile (`target: runtime`, `push: false`, `load: true`); the release version comes from the resolver, not the checkout.
Smoke contract:
- Runs the image, polls `/health` up to 60s, then curls `/ready`.
- Bakes `version("crawlutron")` from `/app/.venv/bin/python` inside the container and asserts it equals `NATIVE_VERSION`.
- Only after the smoke passes: `docker push`, then `actions/attest-build-provenance` with push-to-registry: true.
Exit codes: 0 success (smoke passed, pushed, attested); 2 env/version error; 3 build or smoke failure; 4 push or attestation failure.
Semantic validation: `NATIVE_VERSION` empty (root not released) ⇒ the entrypoint is a no-op, not an error (guarded by `root_version != ''` in release.yml:236).
RELEASE_TAG rule: empty on tag push ⇒ tag images at HEAD's root version; non-empty names a specific tag.

### Dependency SBOM attach (release.yml:318-349)

Name: scripts/release/sbom.py [to be created]
argv: python3 scripts/release/sbom.py (no positional args)
Env contract:
- `TAG`: the package tag (`<name>-v<version>`).
- `GH_TOKEN`: GitHub token for `gh release upload`.
- Permissions required: `contents: write`.
- Builds `sbom.cdx.json` from the frozen lock (`uv export --frozen --no-dev --no-emit-workspace --format requirements-txt`) via `cyclonedx-bom==8.0.1` (`cyclonedx-py requirements ... -o sbom.cdx.json --of json`).
- Uploads `sbom.cdx.json` to the release `$TAG` with `--clobber`.
Exit codes: 0 success; 2 env error; 3 export or SBOM generation failure; 4 upload failure.
Semantic validation: the lock is frozen (`--frozen`); a stale manifest fails immediately rather than producing a mismatched SBOM.
RELEASE_TAG rule: `TAG` is derived from the released entry's tag; empty is not valid here (SBOM always attaches to a concrete tag).

### Distribution artifacts + checksums attach (release.yml:351-384)

Name: scripts/release/release_artifacts.py [to be created]
argv: python3 scripts/release/release_artifacts.py (no positional args)
Env contract:
- `TAG`: the package tag (`<name>-v<version>`).
- `GH_TOKEN`: GitHub token for `gh release upload`.
- Permissions required: `contents: write`.
- Downloads the verified `dist-${selector}` artifact, then in `dist/`: `shopt -s nullglob`, `files=(*.whl *.tar.gz)`, `sha256sum -- "${files[@]}" > SHA256SUMS`.
- Uploads the distributions plus `SHA256SUMS` to `$TAG` with `--clobber`.
Exit codes: 0 success; 2 env error; 3 checksum failure; 4 upload failure.
Semantic validation: when no wheel/tar.gz exists, the upload step fails (no silent empty release).
RELEASE_TAG rule: `TAG` is derived from the released entry's tag; empty is not valid here.

### Install-graph probe + retry (release.yml:194-231)

Name: scripts.release.install_graph [to be created]
argv: python3 -m scripts.release.install_graph (no positional args)
Env contract:
- `NAME`: package name from the released entry.
- `VERSION`: package version from the released entry.
- Creates a clean venv (`python -m venv /tmp/probe`), then retries `pip install --quiet --no-cache-dir "${NAME}==${VERSION}"` up to 30 attempts, sleeping 10s between attempts (index propagation delay tolerance).
- Runs `pip check`; if `NAME == "crawlutron"`, additionally imports `crawlutron`, `crawlutron_extract`, `crawlutron_mcp`, `crawlutron_rest.main`, `crawlutron_sdk`, `safe_url`.
Exit codes: 0 success (installs, pip check passes, optional import probe passes); 2 env error; 3 resolution failure after 30 attempts; 4 import or pip-check failure.
Semantic validation: 30 attempts × 10s bounds the wait; never hangs indefinitely waiting for index propagation.
RELEASE_TAG rule: `NAME`/`VERSION` come from the released entry; empty tag is not valid here (the probe targets a concrete published version).

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
