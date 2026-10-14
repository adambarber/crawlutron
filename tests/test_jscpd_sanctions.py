"""Sanctions name existing guards; imports and prose are not clones."""

from __future__ import annotations

import json

import pytest

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

    `0001_crawl_jobs_initial.py` and `0001_results_initial.py` are flagged as an
    8L/31T clone, and that is the correct shape: migrations are replayed by name
    and two files creating parallel tables is what they are for. A per-file glob
    would need editing every time a migration lands, which is the failure mode
    this whole config is now gated against.
    """
    assert "**/app/storage/migrations/**" in gate.SANCTIONED_IGNORE_GLOBS
    configured = json.loads(gate.CONFIG_PATH.read_text(encoding="utf-8"))["ignore"]
    assert "**/app/storage/migrations/**" in configured


def test_migrations_clones_are_not_reported_but_production_clones_are(monkeypatch, tmp_path, capsys) -> None:
    """One real scan of a mirrored tree, two planted clones: the migrations copy
    is invisible, the production copy is not. Both plants are files this config
    never names, so this also proves a migration added tomorrow is covered by
    the glob.

    The tree is mirrored rather than borrowed, and that is not a convenience.
    This test used to plant the pair at fixed paths in the LIVE `app/` tree and
    unlink them in a `finally`, which made the production tree a directory that
    moves while a suite runs: every concurrent walk of `app/` became a
    list-then-read race, and with 6 concurrent copies of this file against 6
    concurrent copies of tests/invariants/, 10 tests died with
    `FileNotFoundError: .../app/storage/migrations/zz_probe_clone.py` -- the
    tree-wide chokepoint detectors among them, which then reported nothing for
    the whole run while looking green. The glob under test is about path SHAPE,
    so a mirror with the same relative layout is what this needs anyway.
    """
    payload = (gate.SERVICE_ROOT / PROBE_SOURCE).read_text(encoding="utf-8")
    assert len(payload.splitlines()) >= 8, "the probe needs a clone big enough to clear the floor"

    mirror = _mirrored_service_tree(tmp_path, payload)
    _write_expectations(mirror / "expectations.json", [])
    # The real `.jscpd.json` is NOT redirected: the ignore list is the thing under test.
    if (gate.SERVICE_ROOT / ".venv").exists():
        (mirror / ".venv").symlink_to(gate.SERVICE_ROOT / ".venv", target_is_directory=True)
    monkeypatch.setattr(gate, "SERVICE_ROOT", mirror)
    monkeypatch.setattr(gate, "EXPECTATIONS_PATH", mirror / "expectations.json")
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
        gate._reject_source_masking([glob])


@pytest.mark.parametrize(
    "unsanctioned",
    [{"maxLines": 500}, {"skipLocal": True}],
    ids=["maxLines", "skipLocal"],
)
def test_jscpd_json_rejects_unknown_keys(tmp_path, unsanctioned) -> None:
    """Bites: only `ignore` is allowed in .jscpd.json.

    The measured masking, and the reason the check runs before any scan, are
    recorded on ``gate._validate_scan_config`` -- restating them here is a
    second copy to go stale. This test only pins the rejection, and takes its
    cases as a table because the one thing every unsanctioned key has in common
    is being unsanctioned -- which is the rule, spelled once.
    """
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS, **unsanctioned}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown keys"):
        gate._validate_scan_config(config)


def test_jscpd_json_accepts_only_ignore(tmp_path) -> None:
    """A config with only `ignore` passes."""
    config = tmp_path / "jscpd.json"
    config.write_text(
        json.dumps({"ignore": gate.SANCTIONED_IGNORE_GLOBS}),
        encoding="utf-8",
    )
    gate._validate_scan_config(config)


def test_the_test_suite_reads_the_floor_the_hook_runs() -> None:
    """The argv these tests gate on is the hook's own floor, not a copy of it.

    Four spellings of 4/39 used to exist -- the hook entry, the hook's name, this
    module's ``_args_for`` default, and this module's ``_GATE_ARGV`` -- with
    nothing comparing them. The failure that shape hides is not a wrong number, it
    is a green suite: every test here would keep passing against 4/39 while
    commits were gated at whatever the config said, so the suite would vouch for a
    floor that was no longer enforced. Asserting the argv against the config is
    what turns the copy into a derived value.
    """
    argv = _GATE_ARGV
    assert (argv[argv.index("--min-lines") + 1], argv[argv.index("--min-tokens") + 1]) == (
        str(_CONFIGURED_MIN_LINES),
        str(_CONFIGURED_MIN_TOKENS),
    )
    assert _args_for().min_tokens == _CONFIGURED_MIN_TOKENS


def test_a_laxer_floor_is_refused_because_it_cannot_report_the_truth() -> None:
    """A run handed a looser floor than the hook enforces fails instead of passing.

    A duplication gate's number is a command-line argument, so "run it at 8/40" is
    one flag away from the enforced value and returns a genuinely clean result for
    the floor that was asked for. Reporting that as a pass is the whole hazard: the
    output is true and the conclusion is wrong. Only laxer is refused -- scanning
    stricter than the hook is how a candidate floor gets measured before adoption.
    """
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

    The failure this guards is a silent one. If an unreadable floor fell back to a
    default, the gate would go on comparing clones against a number the config
    stopped stating, and the guard refusing lax runs would refuse them against that
    same phantom value -- two checks agreeing on a floor nobody chose.

    The stripped copy is written to ``tmp_path`` and the module is pointed at it,
    because the alternative -- editing the real config to observe that it becomes
    unreadable -- leaves the repository one failed test away from a gate that
    cannot name its own floor.
    """
    stripped = (tmp_path / "config.yaml").write_text(
        gate.PRECOMMIT_CONFIG_PATH.read_text(encoding="utf-8").replace(
            f"--min-tokens {_CONFIGURED_MIN_TOKENS}",
            "",
        ),
        encoding="utf-8",
    )
    assert "--min-tokens" not in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert stripped
    monkeypatch.setattr(gate, "PRECOMMIT_CONFIG_PATH", tmp_path / "config.yaml")
    with pytest.raises(ValueError, match="declares no --min-tokens"):
        gate.configured_floor()


def test_every_sanctioned_pair_names_a_guard_that_exists_and_pins_its_fragment() -> None:
    """The baseline is load-bearing: a sanction is a guard, not a comment.

    A sanction is a standing licence to duplicate, so the baseline is empty and
    that is the claim being asserted. The two pairs it held were the fetcher
    contract and the selector pair, each of which existed only because a
    machine-checked guard required two spellings of one contract; both were
    consolidated, so the licence is no longer needed and is withdrawn rather than
    left in place for the next duplicate to hide behind.

    The loop is the other half and is what fires if a pair is ever sanctioned
    again: a sanction whose guard test was renamed or deleted would leave the copy
    unchecked and the gate green, which is the shape of drift this whole program
    is closing. The hash is what makes a baseline entry a claim about today's
    code -- change either side of a pair and the recorded hash stops matching, so
    the entry fails as unsanctioned rather than continuing to vouch for a fragment
    that no longer exists.
    """
    sanctioned = [pair["id"] for pair in gate._expected_pairs()]
    assert not sanctioned, (
        f"duplicate pairs are sanctioned again: {sanctioned}. A sanction is a standing licence "
        "to duplicate; if a guard genuinely requires two spellings, that is a chokepoint to "
        "build, not a pair to allow. This baseline used to hold two such pairs -- the "
        "HttpFetcher/HttpFetchContract twin signature and the extraction.py selector pair -- and "
        "the first is now one declaration, with the bound keywords checked where each production "
        "fetcher declares them instead of spelled a second time in app/models/taxonomy.py. See "
        "app/extraction/content.py Selectors for the one that remains."
    )
    for pair in gate._expected_pairs():
        guard = pair["guard"]
        path, _, node = guard.partition("::")
        assert node in gate._test_names(gate.SERVICE_ROOT / path), (
            f"{pair['id']} names a guard that is not there"
        )
        assert len(pair["fragment_sha256"]) == 64, f"{pair['id']} has no content pin"


def test_a_fragment_of_imports_alone_is_a_shared_vocabulary_not_a_clone() -> None:
    """Two modules importing the same names is the one identical text that cannot drift.

    Python resolves each module's own namespace, so there is no third module that
    could hold these imports for both, no chokepoint to extract, and no rule for
    the two lists to disagree about. At the enforced floor four such lines clear
    the threshold, so a package whose records each import their own taxonomy
    reported clones forever -- the one shape of duplication that carrying no
    information about where a chokepoint is missing.
    """
    imports = """from app.vocabulary.taxonomy import ResultStatus
from app.models.crawl_job import CrawlJob
from app.models.crawl_result import CrawlResult
from app.models.crawl_worker import CrawlWorker
"""
    assert gate._is_dependency_declaration(imports)


def test_one_line_of_code_between_two_import_runs_keeps_the_pair_under_judgement() -> None:
    """The filter admits a fragment only when EVERY line is an import.

    The duplication this gate exists to find begins at the first line of real
    code, so a fragment that mixes an import run with a body is exactly the
    case that must stay visible -- including when the body is only a signature.
    """
    fragment = """from app.vocabulary.taxonomy import ResultStatus
from app.models.crawl_result import CrawlResult


def build(status: ResultStatus) -> CrawlResult:
    return CrawlResult(status=status)
"""
    assert not gate._is_dependency_declaration(fragment)


def test_a_parenthesised_import_is_still_a_declaration() -> None:
    """A wrapped import is one declaration spread over lines, not several.

    Reading each physical line separately would see ``Classification,`` -- which
    matches no import form -- and drop a fragment that is nothing but imports,
    reinstating the noise the filter exists to remove.
    """
    fragment = """from app.models.taxonomy import (
    Classification,
    CrawlError,
    CrawlStrategyName,
)
"""
    assert gate._is_dependency_declaration(fragment)


def test_a_fragment_ending_inside_an_open_import_is_still_a_declaration() -> None:
    """A run that happens to be cut where the paren opens is still all imports.

    jscpd reports the matched line range, so a fragment can end on the opening
    line of a wrapped import. Judging that by its paren balance asked a question
    about where the report stopped rather than about what the text says, and the
    two test modules that open their shared harness import the same way reported
    a clone of nothing but declarations.
    """
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
