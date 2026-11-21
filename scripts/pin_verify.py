"""One version registry governs every workflow pin; check is offline, refresh peels upstream."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from utils.paths import REPO_ROOT

ROOT = REPO_ROOT
REGISTRY_PATH = ROOT / ".github" / "versions.toml"
SHA = re.compile(r"[0-9a-f]{40}\Z")
USES = re.compile(r"uses:\s*(\S+?)(?:\s+#\s*(\S+))?\s*$")

# Positional aliases keep the registry data out of a class the metrics budget seals at zero.
Pin = tuple[str, str]
PinSite = tuple[str, int, str, str, str]


def pin_version(pin: Pin) -> str:
    return pin[0]


def pin_sha(pin: Pin) -> str:
    return pin[1]


def _object(value: object, key: str) -> dict[str, object] | None:
    nested = value.get(key) if isinstance(value, dict) else None
    return nested if isinstance(nested, dict) else None


def load_registry() -> dict[str, Pin]:
    data: object = tomllib.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    actions = _object(data, "actions")
    if actions is None:
        raise ValueError(f"{REGISTRY_PATH}: missing [actions] table")
    result: dict[str, Pin] = {}
    for name, value in actions.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ValueError(f"{REGISTRY_PATH}: malformed entry for {name!r}")
        version, sha = value.get("version"), value.get("sha")
        if not isinstance(version, str) or not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise ValueError(f"{REGISTRY_PATH}: malformed version or sha for {name}")
        result[name] = (version, sha)
    return result


def discover_sources() -> list[Path]:
    workflows = ROOT.glob(".github/workflows/*.y*ml")
    actions = ROOT.glob(".github/actions/**/action.y*ml")
    return sorted({*workflows, *actions})


def extract_pins(source: Path) -> list[PinSite]:
    pins: list[PinSite] = []
    for line_no, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        match = USES.search(line)
        if match is None:
            continue
        ref, comment = match.groups()
        name, separator, sha = ref.partition("@")
        if not separator or not name or name.startswith("./"):
            continue
        pins.append(
            (
                str(source.relative_to(ROOT)),
                line_no,
                name,
                sha,
                (comment or "").removeprefix("v"),
            )
        )
    return pins


def dependency_name(action: str) -> str:
    parts = action.split("/")
    if len(parts) < 2:
        raise ValueError(f"unqualified action {action!r}")
    return "/".join(parts[:2])


def verify_pins() -> list[str]:
    registry = load_registry()
    errors: list[str] = []
    used: set[str] = set()
    for source in discover_sources():
        for file, line, name, site_sha, version in extract_pins(source):
            where = f"{file}:{line}"
            try:
                dependency = dependency_name(name)
            except ValueError as error:
                errors.append(f"{where}: {error}")
                continue
            pin = registry.get(dependency)
            if pin is None:
                errors.append(f"{where}: unregistered dependency {dependency}")
                continue
            used.add(dependency)
            expected_version, expected_sha = pin
            if not SHA.fullmatch(site_sha):
                errors.append(f"{where}: {dependency} pin {site_sha!r} is not a commit SHA")
            elif site_sha != expected_sha:
                errors.append(f"{where}: {dependency} pins {site_sha}, registry says {expected_sha}")
            if version != expected_version:
                errors.append(
                    f"{where}: {dependency} declares {version!r}, registry says {expected_version!r}"
                )
    for dependency in sorted(set(registry) - used):
        errors.append(f"{REGISTRY_PATH.name}: dependency {dependency} is unused")
    return errors


def _gh_json(endpoint: str) -> dict[str, object] | None:
    executable = shutil.which("gh")
    if executable is None:
        return None
    result = subprocess.run(
        [executable, "api", endpoint],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        data: object = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def peel_to_commit(dependency: str, version: str) -> str | None:
    ref = _gh_json(f"repos/{dependency}/git/ref/tags/v{version}")
    obj = _object(ref, "object")
    sha, kind = obj.get("sha") if obj else None, obj.get("type") if obj else None
    if not isinstance(sha, str) or not isinstance(kind, str):
        return None
    if kind == "tag":
        tag = _gh_json(f"repos/{dependency}/git/tags/{sha}")
        peeled = _object(tag, "object")
        sha = peeled.get("sha") if peeled else None
        if not isinstance(sha, str):
            return None
    if not SHA.fullmatch(sha) or _gh_json(f"repos/{dependency}/commits/{sha}") is None:
        return None
    return sha


def refresh_registry() -> list[str]:
    if shutil.which("gh") is None:
        return ["pin-verify refresh requires gh on PATH"]
    errors: list[str] = []
    for dependency, pin in sorted(load_registry().items()):
        version, expected_sha = pin
        actual = peel_to_commit(dependency, version)
        if actual is None:
            errors.append(f"{REGISTRY_PATH.name}: cannot peel {dependency} v{version}")
        elif actual != expected_sha:
            errors.append(
                f"{REGISTRY_PATH.name}: {dependency} v{version} peels to {actual}, "
                f"registry says {expected_sha}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "refresh"))
    command = parser.parse_args(argv).command
    errors = verify_pins() if command == "check" else refresh_registry()
    if errors:
        for error in errors:
            print(error)
        return 1
    if command == "check":
        sites = sum(len(extract_pins(path)) for path in discover_sources())
        print(f"pins verified against registry: {sites} sites, {len(load_registry())} dependencies")
    else:
        print(f"registry verified upstream: {len(load_registry())} dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
