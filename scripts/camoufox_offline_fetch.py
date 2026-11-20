"""Install the pinned camoufox browser without the rate-limited REST API.

The asset URL and digest are copied from the pinned release's GitHub metadata.
"""

from __future__ import annotations

import camoufox.__main__ as cli
import camoufox.pkgman as pkgman

PIN_TAG = "v158.0.1-beta.41"
PIN_URL = (
    "https://github.com/daijro/camoufox/releases/download/"
    "v158.0.1-beta.41/camoufox-158.0.1-beta.41-lin.x86_64.zip"
)
PIN_SHA256 = "3f2eb1d0451fa58e45e43e55f5e6181565069791ae60c0574f0ceb1f01341c33"
PIN_SIZE = 1_303_577_033


def pinned_versions(repo_config: pkgman.RepoConfig, tag: str) -> list[pkgman.AvailableVersion]:
    """Stand in for the API release lookup with the pinned, hashed asset."""
    del repo_config
    if tag != PIN_TAG:
        raise SystemExit(f"unexpected camoufox pin {tag}; refresh the pinned asset metadata")
    return [
        pkgman.AvailableVersion(
            version=pkgman.Version(build="beta.41", version="158.0.1"),
            url=PIN_URL,
            is_prerelease=True,
            asset_size=PIN_SIZE,
            sha256=PIN_SHA256,
        )
    ]


def main() -> None:
    pin = cli.effective_pin(cli.load_config())
    if pin is None or pin.tag != PIN_TAG:
        raise SystemExit("camoufox changed its browser pin; refresh the pinned asset metadata")
    cli.tagged_versions = pinned_versions
    cli.fetch(None)


if __name__ == "__main__":
    main()
