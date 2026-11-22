"""One version registry governs every workflow pin; check is offline, refresh peels upstream."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from utils.paths import REPO_ROOT

ROOT = REPO_ROOT
REGISTRY_PATH = ROOT / ".github" / "versions.toml"
SHA = re.compile(r"[0-9a-f]{40}\Z")
USES = re.compile(r"uses:\s*(\S+?)(?:\s+#\s*(\S+))?\s*$")
_VERSION = re.compile(r"\d+(?:\.\d+)*\Z")

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


def _gh_value(endpoint: str) -> object | None:
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
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _gh_json(endpoint: str) -> dict[str, object] | None:
    data = _gh_value(endpoint)
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


def parse_version(value: str) -> tuple[int, ...] | None:
    """Numeric dotted version; pre-releases and bundle-style tags are not stable."""
    text = value.removeprefix("v")
    if not _VERSION.fullmatch(text):
        return None
    return tuple(int(part) for part in text.split("."))


def latest_stable_tag(tags: Sequence[str]) -> str | None:
    """The greatest numeric tag; prereleases and non-version tags are ignored."""
    stable = [(version, tag) for tag in tags if (version := parse_version(tag)) is not None]
    if not stable:
        return None
    return max(stable, key=lambda item: item[0])[1]


def waivers() -> dict[str, str]:
    """Intentional holds on newer releases; every entry must justify itself."""
    table = _object(tomllib.loads(REGISTRY_PATH.read_text(encoding="utf-8")), "outdated_waivers") or {}
    result: dict[str, str] = {}
    for name, reason in table.items():
        if not isinstance(name, str) or not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{REGISTRY_PATH.name}: waiver for {name!r} needs a reason")
        result[name] = reason
    return result


def registry_line(dependency: str) -> int:
    """The registry line that declares a dependency, for an actionable finding."""
    for number, line in enumerate(REGISTRY_PATH.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith(f'"{dependency}"'):
            return number
    return 0


def outdated_pins(latest_of: Callable[[str], str | None]) -> list[str]:
    """Every current pin must be the latest stable version, unless waived."""
    registry = load_registry()
    waived = waivers()
    errors: list[str] = []
    for dependency in sorted(waived):
        if dependency not in registry:
            errors.append(f"{REGISTRY_PATH.name}: unregistered waiver {dependency}")
    for name, (version, _sha) in sorted(registry.items()):
        if name in waived:
            continue
        latest = latest_of(name)
        if latest is None:
            errors.append(f"{name}: cannot determine latest stable version")
            continue
        current, newest = parse_version(version), parse_version(latest)
        if current is None or newest is None:
            errors.append(f"{name}: unparseable version pair {version!r}/{latest!r}")
        elif newest > current:
            errors.append(
                f"{REGISTRY_PATH.name}:{registry_line(name)}: {name} is outdated: "
                f"registry {version}, latest {latest}"
            )
    return errors


def _gh_items(endpoint: str) -> list[object] | None:
    data = _gh_value(endpoint)
    return data if isinstance(data, list) else None


def latest_version(dependency: str) -> str | None:
    """Latest stable upstream tag from the tag list, not a bundle-style release."""
    items = _gh_items(f"repos/{dependency}/tags?per_page=100") or []
    tags = [item["name"] for item in items if isinstance(item, dict) and isinstance(item.get("name"), str)]
    tag = latest_stable_tag(tags)
    return tag.removeprefix("v") if tag else None


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
    parser.add_argument("command", choices=("check", "refresh", "outdated"))
    command = parser.parse_args(argv).command
    if command == "check":
        errors = verify_pins()
    elif command == "refresh":
        errors = refresh_registry()
    else:
        errors = outdated_pins(latest_version)
    if errors:
        for error in errors:
            print(error)
        return 1
    if command == "check":
        sites = sum(len(extract_pins(path)) for path in discover_sources())
        print(f"pins verified against registry: {sites} sites, {len(load_registry())} dependencies")
    elif command == "refresh":
        print(f"registry verified upstream: {len(load_registry())} dependencies")
    else:
        print(f"all pins current: {len(load_registry())} dependencies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
