# Changelog

## [0.5.2](https://github.com/adambarber/crawlutron/compare/crawlutron-v0.5.1...crawlutron-v0.5.2) (2026-11-20)


### Bug Fixes

* **ci:** allow a release retry from an existing tag ([10ef5fc](https://github.com/adambarber/crawlutron/commit/10ef5fc0a902c6ce87c164d52d04305a95baa899)), closes [#1185](https://github.com/adambarber/crawlutron/issues/1185)
* **ci:** resolve explicit tags at the workflow commit ([cfa27d3](https://github.com/adambarber/crawlutron/commit/cfa27d354e7abaaa2b62c29329d5afaf94ec2d3a)), closes [#1185](https://github.com/adambarber/crawlutron/issues/1185)
* **ci:** serialize component releases and elect one publisher ([d3039e8](https://github.com/adambarber/crawlutron/commit/d3039e8a33a34b682954129c7c1011d8bd925809)), closes [#1185](https://github.com/adambarber/crawlutron/issues/1185)
* **ci:** trigger releases on component tags ([402b56d](https://github.com/adambarber/crawlutron/commit/402b56d28166e7f2858eb12e6fa4e4f64c74c08f)), closes [#1185](https://github.com/adambarber/crawlutron/issues/1185)
* **release:** publish a complete and attachable distribution ([0c0bee0](https://github.com/adambarber/crawlutron/commit/0c0bee07c114b38481f71faef6c9d71e02574aec)), closes [#1185](https://github.com/adambarber/crawlutron/issues/1185)

## [0.5.1](https://github.com/adambarber/crawlutron/compare/crawlutron-v0.5.0...crawlutron-v0.5.1) (2026-11-20)


### Bug Fixes

* **ci:** bypass proxy and use headed Firefox for browser tests ([a9cadbc](https://github.com/adambarber/crawlutron/commit/a9cadbc165eb969764f9e0da048d5b9b52e63e5b)), closes [#1184](https://github.com/adambarber/crawlutron/issues/1184)
* **ci:** drop apt list cleanup that fails as non-root user ([e2bdb47](https://github.com/adambarber/crawlutron/commit/e2bdb477cf896156e7e104dd3eefa267caf9b367)), closes [#1184](https://github.com/adambarber/crawlutron/issues/1184)
* **ci:** pass GITHUB_TOKEN to fpgen fetch step ([ed3170a](https://github.com/adambarber/crawlutron/commit/ed3170ae518470c19eba3b11ec2e6054e8374b1a)), closes [#1184](https://github.com/adambarber/crawlutron/issues/1184)
* **ci:** pass the build secret as a docker build-push input ([cadb5d3](https://github.com/adambarber/crawlutron/commit/cadb5d3d7a0342a18f84c462c054913e4bcc3349)), closes [#1185](https://github.com/adambarber/crawlutron/issues/1185)
* **ci:** pin fpgen model install ahead of browser tests ([2d96931](https://github.com/adambarber/crawlutron/commit/2d9693143433603368db8dff9c2ccfc0355f5723)), closes [#1184](https://github.com/adambarber/crawlutron/issues/1184)
* **ci:** resolve gitleaks secrets and browser test hermeticity violations ([#1170](https://github.com/adambarber/crawlutron/issues/1170)) ([2e6de5a](https://github.com/adambarber/crawlutron/commit/2e6de5a02d3129690e1f7d3cfb1ca00847886527))
* **ci:** run browser tests under xvfb-run, deduplicate across battery and test-browser ([c94f9d5](https://github.com/adambarber/crawlutron/commit/c94f9d500af85485862bee697968282aa2f7e5b0)), closes [#1184](https://github.com/adambarber/crawlutron/issues/1184)
* **ci:** use libasound2t64 for ubuntu-24.04 runner ([36d748b](https://github.com/adambarber/crawlutron/commit/36d748ba81229cd883c8999f7431cf6f20be4d6a)), closes [#1184](https://github.com/adambarber/crawlutron/issues/1184)
* **ci:** use non-secret test fixture values for gitleaks cleanliness ([#1170](https://github.com/adambarber/crawlutron/issues/1170)) ([ef385d6](https://github.com/adambarber/crawlutron/commit/ef385d6a1f69daab6f54b160aa4cadbbaf2837ea))
* **test:** yield in real_pbkdf2 clamp fixture; browser mandatory, drop optional skip ([e25546e](https://github.com/adambarber/crawlutron/commit/e25546ed01daf2e00eb494b2bf439c7dde3a1ac4)), closes [#1184](https://github.com/adambarber/crawlutron/issues/1184)

## About this changelog

The released entries above are produced by
[release-please](https://github.com/googleapis/release-please) from Conventional
Commits when the owner dispatches `.github/workflows/release-please.yml`. That
workflow is dispatch-only, so between releases this file is the committed record
of what shipped, not a continuously regenerated artifact. History from before
release tooling landed is preserved in `CHANGELOG-legacy.md`.
