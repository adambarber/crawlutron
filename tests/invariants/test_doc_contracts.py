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

    A slash makes the claim directly: only a path with a directory separator
    is a statement about this tree's layout. A bare basename normally is not
    -- ``robots.txt`` in prose is the protocol file, not a file we ship.

    A ``.md`` basename is the exception, and the exception is about what a
    markdown filename *is* rather than about the words around it. Every
    markdown file in this repository is a document of this repository; there
    is no third-party markdown artifact for a document to discuss. So a bare
    ``.md`` span is a pointer at one of our documents whatever verb precedes
    it, and the gate must check it.

    This is the hole that let four live documents point at
    ``ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`` -- a file renamed to
    ``ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md`` -- while the gate that exists
    to catch exactly this reported a clean tree. Restricting the new arm to
    ``.md`` is what keeps it from flagging protocol filenames in prose.
    """
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

    Derived rather than listed, so a document added later is covered by
    default. The earlier version enumerated the nine live documents, which
    made the gate's scope itself a hand-maintained thing that drifts
    silently: a new doc inherited no coverage and nothing failed. An
    exclusion list is the right shape to keep by hand, because every entry is
    a deliberate decision about a record that is *supposed* to cite files
    that no longer exist, and each carries the reason it is exempt.
    """
    return [name for name in _markdown_documents() if name not in _DOC_REFERENCE_EXCLUSIONS]


def _broken_doc_reference_offenders(
    root: pathlib.Path = REPO_ROOT, documents: list[str] | None = None
) -> list[str]:
    """Every repo path a live document tells a reader to open must exist.

    The scope is the point. A record of what was true legitimately cites
    files that have since been deleted, and "fixing" those citations would
    falsify the record -- so the exemptions are named, with reasons, rather
    than applied by an exclusion nobody can see. What is checked is
    everything else: the document a reader arrives intending to follow, where
    a dead path is a navigational dead end and not history.

    The span must look like a repository artifact, and that is not a stylistic
    filter. A bare `robots.txt` or `browser.py` in prose is a basename being
    discussed, not a path being pointed at; only a slash makes the span a
    claim about this tree's layout. A bare ``.md`` span is a claim without a
    slash, because a markdown filename in this tree always names a document of
    this tree -- see `_is_tree_path_claim` for why that arm is bounded to
    markdown.

    ``root`` and ``documents`` exist so the proof that this detector bites can
    run on a copy of a real document instead of on the real one, which is a
    race with every other test that reads the tree.
    """
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

    Compose is how the service is actually deployed, so it is a declaration
    of intent from the same authority as ``.env.example`` -- but a later
    one, and the more consequential: ``.env.example`` describes a local
    checkout, while a key set here is what a real deployment runs with.

    The manifest is a parameter so the proof that this detector bites can
    mutate a copy rather than the repository's own deployment descriptor.
    """
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

    Not a crash and not a 5xx: the deployment starts, the operator believes
    the setting is applied, and the default silently wins. Nothing surfaces
    it, because from the service's point of view the key was never
    mentioned. So the manifest is checked against the same consumer set the
    ``.env.example`` direction uses, rather than trusted.
    """
    declared = _deployment_knobs()
    if declared is None:
        pytest.skip("no compose manifest in this tree")
    dead = sorted(declared - _consumed_knobs())
    assert not dead, f"compose sets knobs nothing reads: {', '.join(dead)}"


def test_deployment_knob_detector_bites(tmp_path):
    """Proved by mutating a real manifest rather than by trusting the helper.

    A copy, and the copy is the point rather than a compromise. The first
    version injected the key into the repository's own ``docker-compose.yml``
    and restored it in a ``finally``; under a parallel run the gate that reads
    that file read it mid-injection and failed on a knob that existed only
    because this test had written it. A gate proved by breaking the artifact
    it protects is not evidence when the proof is the outage.
    """
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

    A document added later has to arrive covered, which is the failure a
    hand-kept list cannot catch. One audit sat outside the original nine for
    exactly as long as it had seven dead paths, and nothing said so -- the
    exclusion set, not the covered set, is what a human maintains now.
    """
    assert set(_live_documents()) | set(_DOC_REFERENCE_EXCLUSIONS) == set(_markdown_documents())
    assert not (stale := set(_DOC_REFERENCE_EXCLUSIONS) - set(_markdown_documents())), stale


def test_live_docs_point_at_paths_that_exist():
    assert not (failures := _broken_doc_reference_offenders()), "; ".join(failures)


def test_live_doc_reference_detector_bites(tmp_path):
    """The gate must be able to fail against a real document, or it is
    decoration rather than a check. Proved by mutating a real document, not by
    feeding the helper a synthetic string.

    On a copy, for the same reason the deployment detector bites on one: this
    test used to inject the dead path into the repository's actual ``README.md``
    and put it back afterwards, which races every other test that reads the
    tree's documents. The text under test is still the real document -- only the
    directory is not -- and that is also why the assertion is containment: a
    bare copy has none of the paths the document cites, and each of those is a
    true positive about the copy rather than a false one.
    """
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
