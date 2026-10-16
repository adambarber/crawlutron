"""Documents point at paths that exist; manifests set no dead knob."""

from __future__ import annotations

import pathlib
import re

import pytest

from tests.chokepoint_walks import REPO_ROOT
from tests.invariants._shared import assert_property_detector_bites
from tests.invariants.test_knob_contracts import (
    _KNOB_PATTERN,
    _consumed_knobs,
    _documented_browser_knobs,
    _documented_knob_offenders,
    _resolved_env_reader_offenders,
    _settings_alias_failures,
)
from tests.property_registry import Property

_DOC_REFERENCE_EXCLUSIONS = {
    "CHANGELOG.md": "a record of what shipped; its citations name modules that were refactored away",
    "ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md": "a dated review quoting the tree it reviewed",
    "docs/review-and-refactoring-plan.md": "stamped ARCHIVED; describes a topology that no longer exists",
    "docs/2026-09-15-performance-review.md": "a dated performance review quoting the tree it measured",
}


_SKIP_DIRECTORIES = frozenset({".git", ".venv", "work", "node_modules", ".pytest_cache", "__pycache__"})


_BACKTICKED_PATH = re.compile(
    # A directory must carry its trailing slash: an optional one would let bare prose such as ``and/or`` read
    # as a path claim, and a gate that reports noise gets disabled rather than obeyed.
    r"^[\w][\w./-]*/[\w./-]*(?:/|(?:\.(?:py|md|toml|json|ya?ml|ini|cfg|txt|in|sh)(?::[\w.]+)?))$"
)


_BACKTICKED_REPO_DOCUMENT = re.compile(r"^[\w][\w-]*\.md$")


def _is_tree_path_claim(candidate: str) -> bool:
    """Whether a backticked span claims something exists in this repository.

    A slash claims the tree; a bare basename does not. Bare ``.md`` is the
    exception -- a renamed-document hole turned the gate's clean report false."""
    return bool(_BACKTICKED_PATH.match(candidate) or _BACKTICKED_REPO_DOCUMENT.match(candidate))


def _markdown_documents() -> list[str]:
    return sorted(
        str(document.relative_to(REPO_ROOT))
        for document in REPO_ROOT.rglob("*.md")
        # Relative parts, never absolute ones: the skip list names directories of THIS repository
        if not _SKIP_DIRECTORIES.intersection(document.relative_to(REPO_ROOT).parts)
    )


def _live_documents() -> list[str]:
    """Every markdown file that is not a stated exception.

    Derived (not enumerated) so a new document arrives covered; the earlier
    nine-file list let the gate's scope drift silently. Exclusions stay hand-kept."""
    return [name for name in _markdown_documents() if name not in _DOC_REFERENCE_EXCLUSIONS]


def _broken_doc_reference_offenders(
    root: pathlib.Path = REPO_ROOT, documents: list[str] | None = None
) -> list[str]:
    """Every repo path a live document tells a reader to open must exist.

    Records of what was true legitimately cite deleted files -- exempted BY
    NAME. The span must claim this tree's layout; the params enable a copy bite."""
    offenders: list[str] = []
    for name in _live_documents() if documents is None else documents:
        document = root / name
        for span in re.findall(r"`([^`\n]+)`", document.read_text(encoding="utf-8")):
            candidate = span.strip()
            if not _is_tree_path_claim(candidate):
                continue
            if not (root / candidate.split(":")[0]).exists():
                offenders.append(f"{name} points at `{candidate}`, which does not exist")
    return offenders


def _compose_file() -> pathlib.Path:
    return REPO_ROOT / "docker-compose.yml"


def _deployment_knobs(manifest: pathlib.Path | None = None) -> set[str] | None:
    """Knobs the compose manifest sets, and the container runs with.

    The later, more consequential declaration than ``.env.example``;
    parameterised so the bite mutates a copy, not the descriptor."""
    manifest = manifest if manifest is not None else _compose_file()
    if not manifest.exists():
        return None
    return {
        name
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
        for name in _KNOB_PATTERN.findall(line)
    }


def test_deployment_manifest_sets_no_dead_knob():
    """A key compose sets that nothing reads is the quietest failure here.

    Not a crash or 5xx: the default silently wins and nothing surfaces it.
    Checked against the same consumer set ``.env.example`` uses."""
    declared = _deployment_knobs()
    if declared is None:
        pytest.skip("no compose manifest in this tree")
    dead = sorted(declared - _consumed_knobs())
    assert not dead, f"compose sets knobs nothing reads: {', '.join(dead)}"


def test_deployment_knob_detector_bites(tmp_path):
    """Proved by mutating a real manifest, not by trusting the helper.

    The first version injected into the repo's own compose file and restored
    in a ``finally``, racing the gate under parallel runs."""
    manifest = _compose_file()
    if not manifest.exists():
        pytest.skip("no compose manifest in this tree")
    probe = tmp_path / manifest.name
    probe.write_text(
        manifest.read_text(encoding="utf-8") + '\n  CRAWLUTRON_TYPO_PROBE: "1"\n',
        encoding="utf-8",
    )
    assert "CRAWLUTRON_TYPO_PROBE" in (_deployment_knobs(probe) or set())
    assert "CRAWLUTRON_TYPO_PROBE" not in (_deployment_knobs() or set())


def test_live_documents_are_derived_not_enumerated():
    """The gate on the gate: coverage must equal the tree.

    A new document must arrive covered -- the failure a hand-kept list cannot
    catch, when one audit sat outside the original nine with seven dead paths."""
    assert set(_live_documents()) | set(_DOC_REFERENCE_EXCLUSIONS) == set(_markdown_documents())
    assert not (stale := set(_DOC_REFERENCE_EXCLUSIONS) - set(_markdown_documents())), stale


def test_live_docs_point_at_paths_that_exist():
    assert not (failures := _broken_doc_reference_offenders()), "; ".join(failures)


def test_live_doc_reference_detector_bites(tmp_path):
    """The gate must be able to fail against a real document, or it is decoration.

    Proved on a COPY: injecting a dead path into the real ``README.md`` raced
    every other reader; the text is still the real document."""
    assert _broken_doc_reference_offenders() == []

    document = REPO_ROOT / "README.md"
    (tmp_path / "README.md").write_text(
        document.read_text(encoding="utf-8") + "\n`app/does_not_exist.py`\n",
        encoding="utf-8",
    )
    offenders = _broken_doc_reference_offenders(root=tmp_path, documents=["README.md"])
    assert "README.md points at `app/does_not_exist.py`, which does not exist" in offenders

    (tmp_path / "README.md").write_text(
        document.read_text(encoding="utf-8") + "\nSee `DOES_NOT_EXIST.md` for the audit trail.\n",
        encoding="utf-8",
    )
    offenders = _broken_doc_reference_offenders(root=tmp_path, documents=["README.md"])
    assert "README.md points at `DOES_NOT_EXIST.md`, which does not exist" in offenders

    # A directory carries no extension, and a symbol suffix is not a line number -- both shapes a reader is
    # still told to open, and both of which a module move breaks exactly as a renamed file does.
    for claim in ("app/does_not_exist_dir/", "app/does_not_exist.py:connect_store"):
        (tmp_path / "README.md").write_text(
            document.read_text(encoding="utf-8") + f"\nSee `{claim}`.\n",
            encoding="utf-8",
        )
        offenders = _broken_doc_reference_offenders(root=tmp_path, documents=["README.md"])
        assert f"README.md points at `{claim}`, which does not exist" in offenders


def test_documented_knob_detector_bites():
    assert _documented_knob_offenders({"CRAWLUTRON_FAKE"}, set()) == [
        "CRAWLUTRON_FAKE has no production consumer"
    ]
    assert _documented_knob_offenders(None, set()) == ["documented knob source could not be located"]
    current = _documented_browser_knobs()
    assert current is not None
    assert "CRAWLUTRON_ALLOWED_HOSTS" in current
    assert not _documented_knob_offenders({"CRAWLUTRON_ALLOWED_HOSTS"}, {"CRAWLUTRON_ALLOWED_HOSTS"})
    assert _documented_knob_offenders({"CRAWLUTRON_LOG_FILE"}, set())
    assert _settings_alias_failures({"missing_field": "CRAWLUTRON_FAKE"}, {"real_field"})
    assert _settings_alias_failures(
        {
            "browser_lazy_scroll_steps": "CRAWLUTRON_FAKE",
            "log_file_path": "CRAWLUTRON_FAKE",
        },
        {"browser_lazy_scroll_steps", "log_file_path"},
    ) == ["browser_lazy_scroll_steps, log_file_path map to CRAWLUTRON_FAKE"]
    prop = Property(
        name="resolved-env-reader",
        owner_module="app/config/settings.py",
        owner_symbols=("from_mapping",),
        definition_site="app/config/settings.py:from_mapping",
        fingerprints=("environment_reader_reference",),
        reason="A documented environment name must resolve through a value reader.",
    )
    assert_property_detector_bites(
        prop,
        _resolved_env_reader_offenders,
        caught={
            "mapping lookup passes field name to reader": (
                "from app.config.settings import ENVIRONMENT_FIELD_ALIASES\n"
                "def broken(source):\n"
                "    return get_mapping_int(source, "
                "ENVIRONMENT_FIELD_ALIASES['CRAWLUTRON_LAZY_SCROLL_STEPS'], 3)\n",
                1,
            ),
            "docstring-only mention": (
                "def read_setting():\n"
                "    '''Mentions CRAWLUTRON_LAZY_SCROLL_STEPS without reading it.'''\n"
                "    return object()\n",
                1,
            ),
        },
        quiet=(
            "from app.config.settings import ENVIRONMENT_FIELD_ALIASES\n"
            "def resolved(source):\n"
            "    return get_mapping_int(source, ENVIRONMENT_FIELD_ALIASES['browser_lazy_scroll_steps'], 3)\n"
        ),
    )
