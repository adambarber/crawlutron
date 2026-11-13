# Release runbook

The version lives in a git tag, nowhere else. Release tooling is
configured today but inert until the repository goes public and GitHub
Actions is restored (#489 ruling: no CI runs against anything while the
repo is private). Everything below is grounded in the live files named.

## Versioning (hatch-vcs)

`[tool.hatch.version] source = "vcs"` in `pyproject.toml` resolves the
version from the newest git tag at build time; the generated
`packages/core/src/crawlutron/_version.py` is a build artifact and is gitignored. There is
no hand-edited version anywhere. Cutting a release is: tag `vX.Y.Z`,
push the tag, build — the sdist/wheel carries that exact version. An
untagged or dirty tree builds with a `dev` suffix instead.

SemVer, honest `0.MINOR.PATCH` until 1.0 (minor may break), matching the
`Development Status :: 4 - Beta` classifier. 1.0 is an event defined by
the exit criteria in #314, not restated here.

## Commit format (commit-msg gate)

`scripts/commit_citation_gate.py` runs at commit-msg and enforces three
laws, each refusal naming the law that fired:

- **Citation**: every commit cites an issue (`Closes #N`).
- **Format**: subject is a Conventional Commit, `type(scope): ...` with
  type in feat/fix/docs/chore/refactor/test/build/ci/perf. `feat:` maps
  to MINOR, `fix:` to PATCH, `feat!:` to MAJOR.
- **Schema**: a `feat!` touching `packages/core/src/crawlutron/models/crawl_policy.py`
  must bump `POLICY_SCHEMA_VERSION` in the same commit.

## Release flow (release-please)

`.github/release-please.yml` triggers
`google-github-actions/release-please-action@v4` on push to main, driven
by `release-please-config.json` (python release type,
`include-component-in-tag: false`, `always-bump-minor` per the 0.x
contract) and `.release-please-manifest.json` (seeded at 0.3.2, the last
hand-written release). Flow once Actions is restored: a merge to main
with shippable commits opens a Release PR; merging the Release PR tags
and publishes. The Release PR diff is the reviewable artifact.

`CHANGELOG.md` is fully machine-generated from Conventional Commit
subjects — do not hand-edit. `CHANGELOG-legacy.md` preserves the
pre-automation history verbatim.

## Manual steps at release time

- Go public; Actions is restored and the release-please workflow activates
  on the next merge to main. Until then it is inert.
- Deploy, fleet concerns, and the Homebrew / npx channels are deferred
  per the #561 owner ruling.

## CI publishes the image when the version changes (#949)

`.github/workflows/release.yml` triggers on a `v*` tag push (the release-please
tag cut) and builds + pushes the root Dockerfile's `runtime` target to GHCR
with the narrow write scope it needs (ci.yml stays `read-all` and cannot push
packages). The image is tagged `:{version}` (lowercased / docker-ref-sanitized)
and `:latest`, and its smoke asserts the baked `crawlutron.__version__` equals
the tag it was pushed as. Creating the next tagged version is: tag `vX.Y.Z`,
push the tag; the release workflow builds and publishes the image.

## Release artifacts

#699: when release-please publishes a release, the release-please workflow
adds one step, gated on `steps.release.outputs.release_created == 'true'`,
that exports the frozen lock
(`uv export --frozen --no-dev --no-emit-workspace --format requirements-txt`,
the same export CI's audit job uses, and `--no-emit-workspace` because the
workspace members are editable local paths an SBOM or an audit cannot resolve), builds a CycloneDX 1.6 JSON SBOM
from it with cyclonedx-bom 8.0.1
(`uv tool run --from cyclonedx-bom==8.0.1 cyclonedx-py requirements ... --of json`),
and uploads `sbom.cdx.json` as an asset on the release
(`gh release upload "$TAG" sbom.cdx.json --clobber`). No provenance
attestation is generated; the SBOM step is the #699 closing change.
