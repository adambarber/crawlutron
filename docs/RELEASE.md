# Release runbook

Each package declares its own version in its `pyproject.toml`, and
`release-please` is the only writer of that number. `release-please-config.json`
names one package per workspace member plus the root aggregator, and
`.release-please-manifest.json` records the last released version of each.
`crawlutron.__version__` reads the installed distribution metadata, so there is
no second copy of the number anywhere in the tree.

SemVer, honest `0.MINOR.PATCH` until 1.0 (minor may break), matching the
`Development Status :: 4 - Beta` classifier. `bump-minor-pre-major` keeps a
breaking change a minor bump while the version is still `0.x`.

## Cutting a release

Nothing is hand-tagged. A merge to `main` runs `.github/workflows/release-please.yml`,
which opens or updates one release PR that bumps only the packages whose files
changed. Merging that PR is the release act: release-please writes the versions,
creates the tags (`<component>-v<version>`), and creates one GitHub release per
package.

The release PR also carries a refreshed `uv.lock`: a version bump makes the lock
stale, and the lock gate runs on every commit after the PR lands.

## What CI does with the tags

`.github/workflows/release.yml` runs on the CI completion of a `main` push and
publishes only the packages whose `<component>-v<version>` tag points at the
commit CI just passed:

| Job | Publishes | Guard |
|---|---|---|
| `released` | nothing (gate only) | `scripts/release_roster.py --released` derives the released set from the tags at HEAD |
| `publish` | one package per matrix job (sdist + wheel) to **PyPI** | asserts the built wheel's `Version` equals the tag, then `twine check --strict`; `skip-existing` resumes a partial upload |
| `install-graph` | nothing | installs the just-published `name==version` from the index in a clean venv and runs `pip check`; for `crawlutron` it also imports every member |
| `image` | the `runtime` image to **GHCR** | only when the root released; runs the image before pushing, asserting the baked `crawlutron.__version__` equals the root version |
| `sbom` | a CycloneDX SBOM attached to each package's release | built from the frozen lock |

Publishing is one matrix job per package with `max-parallel: 1`, so uploads are
spaced by job startup instead of bursting the roster at PyPI and hitting the 429
rate limit that lost `crawlutron-safe-url` and `crawlutron-sdk` in run
43535563524. A re-run of a failed matrix job publishes only that distribution.

A tag that names a version the tree does not declare releases nothing, and a
wheel whose version disagrees with its tag fails before upload, so no artifact
can ship under a version it does not carry.

## Packaging is checked on every commit

`.github/workflows/packaging.yml` runs on push and pull request and publishes
nothing. It builds every member, inspects every artifact with `twine check
--strict`, installs each wheel into its own clean 3.14 environment, and imports
its top level (the import name comes from the wheel's own `__init__.py`
contents, not installed metadata, so `crawlutron-safe-url` imports as
`safe_url`). It also builds the container image without pushing. A packaging break therefore reds before merge, not at release.

The same probe is runnable locally: `uv build --all-packages --out-dir dist` then
`.venv/bin/python scripts/check_packaging.py`.

## One-time PyPI setup (owner)

Publishing uses **trusted publishing** (OIDC) only: there is no stored token,
and the workflow identity is the credential. The `PYPI_API_TOKEN` bootstrap
fallback was removed — there is no long-lived PyPI credential in this repository.

Each project needs one trusted publisher configured on pypi.org. Pending
publishers are deduplicated on the workflow identity and bound to one project
name, consumed by that name's first upload. Register one, release, register the
next: owner `adambarber`, repository `crawlutron`, workflow `release.yml`,
environment `pypi`.

The `pypi` environment requires a reviewer, so no release publishes without a
human click.

## Release security requirements

These are enforced outside the repository (GitHub repo settings) and must be in
place before the first release:

- **Branch protection on `main`**: requires a passing CI `required` check and a
  pull request review from a release owner before any merge.
- **Tag ruleset on `*-v*`**: only repository admins may create or push release
  tags. A tag is the release act — unrestricted tag pushes bypass CI gating.
- **CODEOWNERS**: `.github/CODEOWNERS` lists release-owning paths
  (`.github/workflows/release.yml`, `.github/actions/prepare/action.yml`,
  `scripts/release_roster.py`, `docs/RELEASE.md`); changes to those files
  require release-owner review.
- **`pypi` environment required reviewers**: ≥1 release owner must approve every
  publish.

## Commit format (commit-msg gate)

`scripts/commit_citation_gate.py` runs at commit-msg and enforces three laws, each
refusal naming the law that fired:

- **Citation**: every commit cites an issue (`Closes #N`).
- **Format**: subject is a Conventional Commit, `type(scope): ...` with type in
  feat/fix/docs/chore/refactor/test/build/ci/perf.
- **Schema**: a `feat!` touching
  `packages/core/src/crawlutron/models/crawl_policy.py` must bump
  `POLICY_SCHEMA_VERSION` in the same commit.

`CHANGELOG.md` and `CHANGELOG-legacy.md` are the existing history;
`CHANGELOG-legacy.md` preserves the pre-automation record verbatim. Release
notes come from release-please's changelogs, built from commit subjects.

## Deferred

Deploy, fleet concerns, and the Homebrew / npx channels are out of scope per the
#561 owner ruling.

Image signing (Cosign + SLSA provenance) is deferred: when release automation
reaches that maturity, add Cosign signing to the `image` job and SLSA provenance
to both publish jobs.
