"""The TestPyPI build-skip decision: one pure door, fail-closed."""

from __future__ import annotations

import argparse
import http.client
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import quote, urlparse


def declared_versions(root: Path) -> dict[str, str]:
    """Every distribution this checkout would publish, from its own manifest."""
    manifests = (root / "pyproject.toml", *sorted((root / "packages").glob("*/pyproject.toml")))
    versions: dict[str, str] = {}
    for path in manifests:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        name = project.get("name")
        version = project.get("version")
        if isinstance(name, str) and isinstance(version, str):
            versions[name] = version
    return versions


def should_skip(versions: Mapping[str, str], present: Callable[[str, str], bool]) -> bool:
    """Skip only when every declared distribution already exists at its version."""
    return bool(versions) and all(present(name, version) for name, version in versions.items())


def probe_index(connection: http.client.HTTPConnection, name: str, version: str) -> bool:
    """One exchange on an anchored connection; a miss is not a crash."""
    path = f"/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json"
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status == 200
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def index_present(name: str, version: str, base_url: str) -> bool:
    """Index probe over HTTPS only; the transport is never downgraded."""
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("the index URL must be https with a host")
    connection = http.client.HTTPSConnection(parsed.hostname, port=parsed.port, timeout=10)
    return probe_index(connection, name, version)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-url", default="https://test.pypi.org")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    versions = declared_versions(args.root)
    skip = should_skip(
        versions, lambda name, version: index_present(name, version, args.index_url.rstrip("/"))
    )
    print("true" if skip else "false")


if __name__ == "__main__":
    main()
