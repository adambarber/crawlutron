"""Doc-drift claims derive from live release, project, and workflow source."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from tests._ci_contract import workflow_files
from tests.chokepoint_paths import REPO_ROOT

_MANIFEST = REPO_ROOT / ".release-please-manifest.json"
_PROJECT = REPO_ROOT / "pyproject.toml"
_SECURITY = REPO_ROOT / "SECURITY.md"
_CHANGELOG = REPO_ROOT / "CHANGELOG.md"
_NOTICE = REPO_ROOT / "NOTICE"
_ADOPTION = ("SUPPORT.md", "GOVERNANCE.md", "FUNDING.md")

_BEGIN = "<!-- SUPPORTED_VERSIONS:BEGIN -->"
_END = "<!-- SUPPORTED_VERSIONS:END -->"
_BACKTICK = re.compile(r"`([^`\n]+)`")
_VERSION_LINE = re.compile(r"\b\d+\.\d+\.x\b")
_OWNER = re.compile(r"@[A-Za-z0-9][A-Za-z0-9-]*")

_BANNED_CLAIMS = (
    re.compile(r"github\.com/sponsors", re.I),
    re.compile(r"opencollective\.com", re.I),
    re.compile(r"buymeacoffee\.com", re.I),
    re.compile(r"patreon\.com", re.I),
    re.compile(r"img\.shields\.io/(?:sponsor|patreon|opencollective)", re.I),
    re.compile(r"\b\d+(?:\.\d+)?% of (?:users|customers|traffic)\b", re.I),
    re.compile(r"\bbypass\s+(?:Cloudflare|WAF|CAPTCHA|anti-bot)", re.I),
)
_STALE_CHANGELOG_CLAIMS = ("Fully machine-generated", "do not hand-edit")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _project() -> dict[str, object]:
    return dict(tomllib.loads(_read(_PROJECT))["project"])


def _live_version() -> str:
    version = str(json.loads(_read(_MANIFEST))["."])
    assert version == _project()["version"], "manifest and declared project version disagree"
    return version


def _live_line() -> str:
    major, minor, _patch = _live_version().split(".")
    return f"{major}.{minor}"


def _expected_block() -> str:
    line = _live_line()
    return "\n".join(
        (
            _BEGIN,
            "| Version | Supported     |",
            "|---------|---------------|",
            f"| {line}.x   | Yes (current) |",
            f"| < {line}   | No            |",
            _END,
        )
    )


def _extract_block(text: str) -> str | None:
    lines = text.splitlines()
    if _BEGIN not in lines:
        return None
    start = lines.index(_BEGIN)
    if _END not in lines[start + 1 :]:
        return None
    end = lines.index(_END, start + 1)
    return "\n".join(lines[start : end + 1])


def _version_offenders(text: str) -> list[str]:
    actual = _extract_block(text)
    if actual is None:
        return ["SECURITY.md: supported-version marker block is missing"]
    lines = text.splitlines()
    start = lines.index(_BEGIN)
    end = lines.index(_END, start + 1)
    offenders: list[str] = []
    if actual != _expected_block():
        offenders.append(f"SECURITY.md:{start + 1}: table drifted from live release line {_live_line()}")
    for number, raw in enumerate(lines, start=1):
        if start + 1 <= number <= end + 1:
            continue
        if _VERSION_LINE.search(raw):
            offenders.append(f"SECURITY.md:{number}: version literal outside the derived block")
    return offenders


def _stale_line_text(text: str) -> str:
    major, minor, _patch = _live_version().split(".")
    stale = f"{major}.{int(minor) + 1}.x"
    return text.replace(f"| {_live_line()}.x", f"| {stale}", 1)


def _urls() -> dict[str, object]:
    value = _project()["urls"]
    assert isinstance(value, dict)
    return {str(key): item for key, item in value.items()}


def _maintainers(root: Path) -> tuple[str, ...]:
    """Owners cited by the live CODEOWNERS file, derived not hand-maintained."""
    text = _read(root / ".github" / "CODEOWNERS")
    owners = {
        owner
        for raw in text.splitlines()
        if raw.strip() and not raw.lstrip().startswith("#")
        for owner in _OWNER.findall(raw)
    }
    return tuple(sorted(owners))


def _maintainer_line(lines: list[str]) -> int:
    """Line carrying the maintainer claim, for a file:line bite on a stale owner."""
    return next((number for number, raw in enumerate(lines, start=1) if "maintainer" in raw.lower()), 1)


def _required_tokens(root: Path = REPO_ROOT) -> dict[str, tuple[str, ...]]:
    raw_urls = _urls()
    urls = {key: str(value) for key, value in raw_urls.items()}
    license_name = str(_project()["license"])
    return {
        "SUPPORT.md": (urls["Issues"], urls["Security"], license_name),
        "GOVERNANCE.md": (urls["Issues"], license_name, *_maintainers(root)),
        "FUNDING.md": (license_name,),
    }


def _adoption_offenders(root: Path) -> list[str]:
    offenders: list[str] = []
    maintainers = _maintainers(root)
    for name, tokens in _required_tokens(root).items():
        path = root / name
        if not path.is_file():
            offenders.append(f"{name}: adoption surface is missing")
            continue
        lines = _read(path).splitlines()
        for token in tokens:
            if not any(token in line for line in lines):
                where = f"{name}:{_maintainer_line(lines)}" if token in maintainers else name
                offenders.append(f"{where}: does not cite live token {token!r}")
        for number, raw in enumerate(lines, start=1):
            if any(pattern.search(raw) for pattern in _BANNED_CLAIMS):
                offenders.append(f"{name}:{number}: banned sponsor, metric, or bypass claim")
    return offenders


def _write_valid_adoption(root: Path) -> None:
    for name, tokens in _required_tokens().items():
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(
            "\n".join(f"Live claim: {token}" for token in tokens) + "\n", encoding="utf-8"
        )
    codeowners = root / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir(parents=True, exist_ok=True)
    codeowners.write_text(_read(REPO_ROOT / ".github" / "CODEOWNERS"), encoding="utf-8")


def _notice_dead_paths(text: str) -> list[str]:
    offenders: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        for span in _BACKTICK.findall(raw):
            token = span.strip()
            if "/" not in token or token.startswith(("http://", "https://")):
                continue
            path = token.split()[0]
            if not (REPO_ROOT / path).exists():
                offenders.append(f"NOTICE:{number}: cites dead path {path}")
    return offenders


def _release_workflow() -> str:
    root = REPO_ROOT / ".github" / "workflows"
    hits = [path for path in workflow_files(root) if "release-please-action@" in _read(path)]
    assert len(hits) == 1, f"expected one release-please workflow, found {len(hits)}"
    return hits[0].relative_to(REPO_ROOT).as_posix()


def _changelog_offenders(text: str) -> list[str]:
    offenders = [
        f"CHANGELOG.md:{number}: stale release claim {claim!r}"
        for number, raw in enumerate(text.splitlines(), start=1)
        for claim in _STALE_CHANGELOG_CLAIMS
        if claim in raw
    ]
    workflow = _release_workflow()
    if workflow not in text:
        offenders.append(f"CHANGELOG.md: does not name the live release workflow {workflow}")
    return offenders


def test_supported_versions_block_matches_the_live_release_line():
    assert not (failures := _version_offenders(_read(_SECURITY))), "; ".join(failures)


def test_supported_versions_bites_a_stale_line():
    stale = _stale_line_text(_read(_SECURITY))
    assert any("SECURITY.md:" in hit and "drifted" in hit for hit in _version_offenders(stale)), (
        _version_offenders(stale)
    )


def test_supported_versions_bites_a_flipped_support_flag():
    stale = _read(_SECURITY).replace("Yes (current)", "No", 1)
    assert any("SECURITY.md:" in hit and "drifted" in hit for hit in _version_offenders(stale)), (
        _version_offenders(stale)
    )


def test_supported_versions_bites_a_missing_block():
    stale = _read(_SECURITY).replace(_BEGIN, "", 1)
    assert _version_offenders(stale) == ["SECURITY.md: supported-version marker block is missing"]


def test_supported_versions_bites_a_version_literal_in_prose():
    major, _minor, _patch = _live_version().split(".")
    literal = f"{int(major) + 1}.0.x"
    stale = _read(_SECURITY).replace("\n## Reporting", f"\n{literal}\n## Reporting", 1)
    assert any("version literal outside the derived block" in hit for hit in _version_offenders(stale)), (
        _version_offenders(stale)
    )


def test_adoption_surfaces_cite_live_source_and_carry_no_banners():
    assert not (failures := _adoption_offenders(REPO_ROOT)), "; ".join(failures)


def test_adoption_gate_bites_a_missing_file(tmp_path):
    _write_valid_adoption(tmp_path)
    (tmp_path / "GOVERNANCE.md").unlink()
    assert _adoption_offenders(tmp_path) == ["GOVERNANCE.md: adoption surface is missing"]


def test_adoption_gate_bites_a_dead_contact_url(tmp_path):
    _write_valid_adoption(tmp_path)
    path = tmp_path / "SUPPORT.md"
    text = _read(path)
    path.write_text(
        text.replace(_required_tokens()["SUPPORT.md"][0], "https://example.invalid/issues"), encoding="utf-8"
    )
    assert any("SUPPORT.md: does not cite live token" in hit for hit in _adoption_offenders(tmp_path)), (
        _adoption_offenders(tmp_path)
    )


def test_adoption_gate_bites_a_sponsor_banner(tmp_path):
    _write_valid_adoption(tmp_path)
    path = tmp_path / "FUNDING.md"
    path.write_text(
        _read(path) + "\n[![sponsor](https://img.shields.io/sponsor)](https://github.com/sponsors/x)\n",
        encoding="utf-8",
    )
    assert any("sponsor, metric, or bypass claim" in hit for hit in _adoption_offenders(tmp_path)), (
        _adoption_offenders(tmp_path)
    )


def test_notice_cites_no_dead_source_paths():
    assert not (failures := _notice_dead_paths(_read(_NOTICE))), "; ".join(failures)


def test_notice_gate_bites_a_planted_dead_path():
    text = _read(_NOTICE) + "\nSee `app/removed/former.py`.\n"
    assert any("NOTICE:" in hit and "dead path" in hit for hit in _notice_dead_paths(text)), (
        _notice_dead_paths(text)
    )


def test_changelog_names_the_live_release_workflow():
    assert not (failures := _changelog_offenders(_read(_CHANGELOG))), "; ".join(failures)


def test_changelog_gate_bites_a_reintroduced_generation_claim():
    stale = _read(_CHANGELOG) + "\nFully machine-generated; do not hand-edit.\n"
    assert any("stale release claim" in hit for hit in _changelog_offenders(stale)), _changelog_offenders(
        stale
    )


def test_changelog_gate_bites_a_dead_workflow_citation():
    stale = _read(_CHANGELOG).replace(_release_workflow(), ".github/workflows/gone.yml")
    assert any("does not name the live release workflow" in hit for hit in _changelog_offenders(stale)), (
        _changelog_offenders(stale)
    )


def _scratch_ownership(root: Path, owner: str, claim_owner: str) -> None:
    """Seed a scratch adoption tree with a CODEOWNERS owner and governance claim."""
    _write_valid_adoption(root)
    governance = root / "GOVERNANCE.md"
    governance.write_text(
        _read(governance) + f"\nthe maintainer is {claim_owner}, as recorded in CODEOWNERS.\n",
        encoding="utf-8",
    )
    codeowners = root / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir(parents=True, exist_ok=True)
    codeowners.write_text(f"jscpd-expectations.json {owner}\n", encoding="utf-8")


def test_adoption_gate_bites_an_ownership_transfer(tmp_path):
    _scratch_ownership(tmp_path, "@newmaintainer", "@adambarber")
    failures = _adoption_offenders(tmp_path)
    assert any(re.search(r"GOVERNANCE\.md:\d+.*@newmaintainer", hit) for hit in failures), failures


def test_adoption_gate_follows_a_transferred_owner_when_governance_updates(tmp_path):
    _scratch_ownership(tmp_path, "@newmaintainer", "@newmaintainer")
    assert not _adoption_offenders(tmp_path), _adoption_offenders(tmp_path)
