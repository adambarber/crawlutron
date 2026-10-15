"""Sanctions name existing guards; imports and prose are not clones."""

from __future__ import annotations

import json

import pytest

from scripts import _jscpd_pair_resolution as res
from scripts import jscpd_gate as gate
from tests.test_jscpd_floors import _configure_scan_config
from tests.test_jscpd_gate import (
    _CONFIGURED_MIN_LINES,
    _CONFIGURED_MIN_TOKENS,
    _GATE_ARGV,
    PROBE_SOURCE,
    _args,
    _args_for,
    _mirrored_service_tree,
    _write_expectations,
)


def test_migrations_exclusion_is_a_directory_glob_so_new_migrations_are_covered() -> None:
    """The exclusion is a subtree, not a list of today's migration files.

    Two parallel-table migrations flag as a clone at 8L/31T, rightly; a
    per-file glob would need editing every time one lands."""
    assert "**/app/storage/migrations/**" in gate.SANCTIONED_IGNORE_GLOBS
    configured = json.loads(gate.CONFIG_PATH.read_text(encoding="utf-8"))["ignore"]
    assert "**/app/storage/migrations/**" in configured


def test_migrations_clones_are_not_reported_but_production_clones_are(monkeypatch, tmp_path, capsys) -> None:
    """One real scan of a mirrored tree, two planted clones: the migrations copy
    is invisible, the production copy is not. The mirror -- not the live app --
    stops concurrent walks from racing the real tree."""
    payload = (gate.SERVICE_ROOT / PROBE_SOURCE).read_text(encoding="utf-8")
    assert len(payload.splitlines()) >= 8, "the probe needs a clone big enough to clear the floor"

    mirror = _mirrored_service_tree(tmp_path, payload)
    _write_expectations(mirror / "expectations.json", [])
    # The real `.jscpd.json` is NOT redirected: the ignore list is the thing under test.
    if (gate.SERVICE_ROOT / ".venv").exists():
        (mirror / ".venv").symlink_to(gate.SERVICE_ROOT / ".venv", target_is_directory=True)
    monkeypatch.setattr(res, "SERVICE_ROOT", mirror)
    monkeypatch.setattr(res, "EXPECTATIONS_PATH", mirror / "expectations.json")
    monkeypatch.setattr(gate, "MIN_SOURCES", {"all": 1})

    assert gate._run(_args()) == 1

    reported = capsys.readouterr().err
    assert "app/zz_probe_clone.py" in reported, f"a production clone must be reported:\n{reported}"
    assert "migrations/zz_probe_clone.py" not in reported, (
        f"a migration clone must be excluded by the glob, not reported:\n{reported}"
    )


@pytest.mark.parametrize("drift", ["added-glob", "removed-glob", "whole-tree-glob"])
def test_unowned_ignore_list_fails_before_any_scan(monkeypatch, tmp_path, drift) -> None:
    sanctioned = gate.SANCTIONED_IGNORE_GLOBS
    if drift == "added-glob":
        ignore = [*sanctioned, "**/zz_probe_clone.py"]
    elif drift == "removed-glob":
        ignore = list(sanctioned[:-1])
    else:
        ignore = [*sanctioned, "**"]

    commands = _configure_scan_config(monkeypatch, tmp_path, ignore)
    assert gate.main() == 1
    assert commands == []


@pytest.mark.parametrize("glob", ["**/*.py", "app/*.py", "**/zz_probe_clone.py", "*", "**"])
def test_python_source_globs_are_never_a_sanctioned_exception(glob) -> None:
    """The ignore list exists to skip caches and vendored trees, never source."""
    with pytest.raises(ValueError, match="Python source|whole tree"):
        res._reject_source_masking([glob])


@pytest.mark.parametrize(
    "unsanctioned",
    [{"maxLines": 500}, {"skipLocal": True}],
    ids=["maxLines", "skipLocal"],
)
def test_jscpd_json_rejects_unknown_keys(tmp_path, unsanctioned) -> None:
    """Bites: only ``ignore`` is allowed in .jscpd.json.

    The measured masking and the reason the check runs first live on
    ``gate._validate_scan_config``; this pins the rejection, case-tabled."""
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS, **unsanctioned}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        res._validate_scan_config(config)


def test_jscpd_json_accepts_only_ignore(tmp_path) -> None:
    """A config with only `ignore` passes."""
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS}),
        encoding="utf-8",
    )
    res._validate_scan_config(config)


def test_the_test_suite_reads_the_floor_the_hook_runs() -> None:
    """The argv these tests gate on is the hook's own floor, not a copy of it.

    Four spellings of 4/39 used to exist with nothing comparing them; deriving
    the argv from the config kills the green-suite-against-a-different-floor lie."""
    argv = _GATE_ARGV
    assert (argv[argv.index("--min-lines") + 1], argv[argv.index("--min-tokens") + 1]) == (
        str(_CONFIGURED_MIN_LINES),
        str(_CONFIGURED_MIN_TOKENS),
    )
    assert _args_for().min_tokens == _CONFIGURED_MIN_TOKENS


def test_a_laxer_floor_is_refused_because_it_cannot_report_the_truth() -> None:
    """A run handed a looser floor than the hook enforces fails instead of passing.

    The floor is a CLI argument, so a lax run returns a genuinely clean result
    for what was asked -- true output, wrong conclusion. Only laxer is refused."""
    laxer = _args_for(
        min_lines=_CONFIGURED_MIN_LINES + 1,
        min_tokens=_CONFIGURED_MIN_TOKENS + 1,
    )
    with pytest.raises(ValueError, match="laxer than the enforced floor"):
        gate._require_configured_floor(laxer)


def test_the_enforced_floor_and_a_stricter_one_are_both_accepted() -> None:
    """The floor itself, and anything below it, are legitimate runs."""
    gate._require_configured_floor(_args_for())
    gate._require_configured_floor(
        _args_for(min_lines=_CONFIGURED_MIN_LINES, min_tokens=_CONFIGURED_MIN_TOKENS - 1)
    )


def test_a_floor_the_hook_does_not_state_is_unreadable_rather_than_defaulted(monkeypatch, tmp_path) -> None:
    """Bites: a hook entry missing a threshold raises instead of reading as one.

    A fallback default would let two checks agree on a floor nobody chose; the
    stripped copy lives in ``tmp_path``, never in the real config."""
    stripped = (tmp_path / "config.yaml").write_text(
        gate.PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8").replace(
            f"--min-tokens {_CONFIGURED_MIN_TOKENS}",
            "",
        ),
        encoding="utf-8",
    )
    assert "--min-tokens" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert stripped
    monkeypatch.setattr(res, "PRECOMMIT_CONFIG_PATH", tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="declares no --min-tokens"):
        gate.configured_floor()


def test_every_sanctioned_pair_names_a_guard_that_exists_and_pins_its_fragment() -> None:
    """The baseline is load-bearing: a sanction is a guard, not a comment.

    Empty today -- the pairs it held were consolidated. A future sanction must
    name a guard test that exists and a hash matching today's fragment."""
    sanctioned = [pair["id"] for pair in res._expected_pairs()]
    assert not sanctioned, (
        f"duplicate pairs are sanctioned again: {sanctioned}. A sanction is a standing licence "
        "to duplicate; if a guard genuinely requires two spellings, that is a chokepoint to "
        "build, not a pair to allow. This baseline used to hold two such pairs -- the "
        "HttpFetcher/HttpFetchContract twin signature and the extraction.py selector pair -- and "
        "the first is now one declaration, with the bound keywords checked where each production "
        "fetcher declares them instead of spelled a second time in app/models/taxonomy.py. See "
        "app/extraction/content.py Selectors for the one that remains."
    )
    for pair in res._expected_pairs():
        guard = pair["guard"]
        path, _, node = guard.partition("::")
        assert node in res._test_names(gate.SERVICE_ROOT / path), (
            f"{pair['id']} names a guard that is not there"
        )
        assert len(pair["fragment_sha256"]) == 64, f"{pair['id']} has no content pin"


def test_a_fragment_of_imports_alone_is_a_shared_vocabulary_not_a_clone() -> None:
    """Two modules importing the same names is the one identical text that cannot drift.

    No third module could hold the imports, so there is no chokepoint to extract
    and no rule they could disagree about; four such lines clear the floor."""
    imports = """from app.vocabulary.taxonomy import ResultStatus
from app.models.crawl_job import CrawlJob
from app.models.crawl_result import CrawlResult
from app.models.crawl_worker import CrawlWorker
"""
    assert gate._is_dependency_declaration(imports)


def test_one_line_of_code_between_two_import_runs_keeps_the_pair_under_judgement() -> None:
    """The filter admits a fragment only when EVERY line is an import.

    The duplication under test begins at the first line of real code, so a
    fragment mixing an import run with a body -- even a signature -- stays visible."""
    fragment = """from app.vocabulary.taxonomy import ResultStatus
from app.models.crawl_result import CrawlResult

def build(status: ResultStatus) -> CrawlResult:
    return CrawlResult(status=status)
"""
    assert not gate._is_dependency_declaration(fragment)


def test_a_parenthesised_import_is_still_a_declaration() -> None:
    """A wrapped import is one declaration spread over lines, not several.

    Reading physical lines would see ``Classification,`` -- matching no import
    form -- and drop a fragment that is nothing but imports."""
    fragment = """from app.models.taxonomy import (
    Classification,
    CrawlError,
    CrawlStrategyName,
)
"""
    assert gate._is_dependency_declaration(fragment)


def test_a_fragment_ending_inside_an_open_import_is_still_a_declaration() -> None:
    """A run cut where the paren opens is still all imports.

    jscpd reports the matched line range, so the fragment can end on an opening
    paren; paren balance asks where the report stopped, not what the text says."""
    fragment = """from app.storage.job_store import FrontierEntry, JobStatus
from app.admission.policy import read_policy
from app.models.crawl_policy import CrawlPolicy
from app.config.settings import CrawlerSettings
from tests._job_harnesses import (
"""
    assert gate._is_dependency_declaration(fragment)


def test_prose_repeated_in_two_docstrings_is_still_a_clone() -> None:
    """The same argument written twice is duplication, and the filter must not excuse it.

    Two docstrings stating the same reasoning have two homes that can disagree,
    which is precisely what a one-home rule exists to prevent.
    """
    fragment = '''"""Two functions rather than one with a cast inside the loop."""
    """
    return value
'''
    assert not gate._is_dependency_declaration(fragment)
