"""Public doc claims stay derived from live enums, manifests, and generators."""

from __future__ import annotations

import pathlib
import re
from functools import lru_cache

from crawlutron.models.crawl_extraction_method import CrawlExtractionMethod
from crawlutron.vocabulary.extraction import EXTRACTION_DESCRIPTIONS
from crawlutron.vocabulary.taxonomy import CrawlClassification
from crawlutron_mcp.registry import TOOL_SPECS
from scripts import module_graph
from tests._walk_primitives import discovered_files
from tests.chokepoint_paths import REPO_ROOT

_CLASSIFICATION_COUNT_CLAIMS = (
    re.compile(r"\b(\d+)\s+crawl\s+classifications\b"),
    re.compile(r"\b(\d+)\s+classifications\b"),
    re.compile(r"\(all\s+(\d+)\)"),
    re.compile(r"enum\s+of\s+\*\*(\d+)\s+values\*\*"),
    re.compile(r"\((\d+)-value\s+classification\s+enum\)"),
)
_TOOL_COUNT_CLAIM = re.compile(r"\b(\d+)\s+(?:MCP\s+)?tools\b")
_TOOL_ENUMERATION = re.compile(r"exposes\s+([^\n.]+?)\s+as\s+tools")


def _read(relative: str) -> str:
    """Read a tracked doc through the shared filesystem door."""
    path = REPO_ROOT / relative
    found = discovered_files(path.parent, path.name, include_content=True)
    assert isinstance(found, dict) and path in found, f"{relative}: document is missing"
    return found[path]


def _readme() -> str:
    return _read("README.md")


def _manual_plan() -> str:
    return _read("docs/references/crawlutron-manual-test-plan.md")


def _live_counts() -> tuple[int, int]:
    return len(list(CrawlClassification)), len(TOOL_SPECS)


def _stated_count_offenders(text: str, document: str = "README") -> list[str]:
    """Every hardcoded taxonomy/tool count, with the live value it should defer to."""
    classifications, tools = _live_counts()
    offenders = [
        f"{document} hardcodes {match.group(1)} classifications; live enum has {classifications}"
        for pattern in _CLASSIFICATION_COUNT_CLAIMS
        for match in pattern.finditer(text)
    ]
    offenders.extend(
        f"{document} hardcodes {match.group(1)} MCP tools; live registry has {tools}"
        for match in _TOOL_COUNT_CLAIM.finditer(text)
    )
    return offenders


def _listed_tool_offenders(text: str) -> list[str]:
    """A hand-listed tool set that is not the live registry drifts like a stale count."""
    names = {spec.name for spec in TOOL_SPECS}
    offenders = []
    for match in _TOOL_ENUMERATION.finditer(text):
        listed = {
            token for token in re.findall(r"[a-z][a-z_]+", match.group(1).replace("`", "")) if token in names
        }
        if listed and listed != names:
            offenders.append(f"README hand-lists MCP tools {sorted(listed)}; tools/list is authoritative")
    return offenders


def test_readme_states_no_hardcoded_classification_or_tool_counts():
    assert not (failures := _stated_count_offenders(_readme())), "; ".join(failures)


def test_manual_plan_states_no_hardcoded_classification_count():
    assert not (failures := _stated_count_offenders(_manual_plan(), "manual plan")), "; ".join(failures)


def test_manual_plan_points_at_the_live_classification_contract():
    text = _manual_plan()
    assert "supported_classifications" in text
    assert "CrawlClassification" in text


def test_readme_does_not_hand_list_the_mcp_tool_set():
    assert not (failures := _listed_tool_offenders(_readme())), "; ".join(failures)


def test_readme_points_at_live_contract_sources():
    text = _readme()
    assert "supported_classifications" in text
    assert "tools/list" in text


def test_stale_claim_detectors_bite(tmp_path: pathlib.Path):
    """A planted stale count and a stale tool set must both be named by the gates."""
    stale = (
        "- **Content classification** — 36 crawl classifications (rendered, blocked, etc.)\n"
        "- **MCP** — `POST /mcp` exposes search/crawl/map/render as tools.\n"
        "Every result carries a classification from a fixed enum of **36 values**.\n"
        "taxonomy.py (36-value classification enum), strategy names\n"
        "`POST /mcp` exposes 4 tools over the Model Context Protocol.\n"
    )
    path = tmp_path / "README.md"
    path.write_text(stale, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    offenders = _stated_count_offenders(text)
    assert "README hardcodes 36 classifications; live enum has 40" in offenders
    assert "manual plan hardcodes 36 classifications; live enum has 40" in (
        _stated_count_offenders("all 36 classifications (see §7)", "manual plan")
    )
    assert "README hardcodes 4 MCP tools; live registry has 18" in offenders
    assert len(offenders) == 4, offenders
    assert any("hand-lists MCP tools" in item for item in _listed_tool_offenders(text))


_ACTIVE_DOCS = (
    "README.md",
    "SECURITY.md",
    "docs/RELEASE.md",
    "docs/architecture-governance.md",
    "docs/references/crawlutron-agent-contract.md",
    "docs/references/crawlutron-manual-test-plan.md",
    "docs/module-graph.md",
)
_BACKTICK = re.compile(r"`[^`\n]*`")
_REMOVED_LIBS = ("markitdown", "markdownify")
_DELETED_METHOD = "trafilatura"
_LIVE_DELETED_METHOD_SOURCES = (
    "NOTICE",
    "packages/core/src/crawlutron/vocabulary/extraction.py",
    "packages/core/src/crawlutron/models/crawl_extraction_method.py",
    "packages/core/src/crawlutron/models/crawl_policy.py",
    "packages/core/src/crawlutron/models/unified_crawl_result.py",
    "packages/core/src/crawlutron/transport/http_classify.py",
    "packages/rest/src/crawlutron_rest/main.py",
    "packages/rest/src/crawlutron_rest/api/crawl_router.py",
    "packages/rest/src/crawlutron_rest/cli/__main__.py",
    "packages/rest/src/crawlutron_rest/discovery/agent_hints.py",
    "packages/rest/src/crawlutron_rest/discovery/capabilities.py",
    "packages/mcp/src/crawlutron_mcp/registry.py",
)
_FAIL_UNDER = re.compile(r"--cov-fail-under=(\d+)")
_SMOKE_VERSION = re.compile(r"version\(['\"]crawlutron['\"]\)")


def _removed_from_lock() -> tuple[str, ...]:
    lock = _read("uv.lock").lower()
    return tuple(name for name in _REMOVED_LIBS if f'name = "{name}"' in lock)


def _stale_extraction_offenders(text: str, document: str) -> list[str]:
    offenders: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        prose = _BACKTICK.sub("", raw).lower()
        for name in _REMOVED_LIBS:
            if re.search(rf"\b{name}\b", prose):
                offenders.append(f"{document}:{number}: names removed extraction library {name!r}")
    return offenders


def _description_offenders(descriptions: dict[str, str]) -> list[str]:
    return [
        f"EXTRACTION_DESCRIPTIONS[{key}]: names removed library {name!r}"
        for key, description in descriptions.items()
        for name in _REMOVED_LIBS
        if re.search(rf"\b{name}\b", description, re.I)
    ]


def _production_offenders() -> list[str]:
    sources = (
        "packages/core/src/crawlutron/vocabulary/extraction.py",
        "packages/core/src/crawlutron/models/crawl_policy.py",
        "packages/core/src/crawlutron/models/crawl_extraction_method.py",
    )
    return [
        f"{relative}: names removed library {name!r}"
        for relative in sources
        for name in ("markitdown", "markdownify")
        if re.search(rf"\b{name}\b", _read(relative), re.I)
    ]


def _coverage_offenders(text: str, document: str) -> list[str]:
    match = _FAIL_UNDER.search(_read(".pre-commit-config.yaml"))
    if match is None:
        return [".pre-commit-config.yaml: no --cov-fail-under floor to document"]
    if "[![codecov]" not in _read("README.md"):
        return []
    offenders: list[str] = []
    if re.search(r"no coverage badge", text, re.I):
        offenders.append(f"{document}: denies the live codecov badge")
    if f"{match.group(1)}%" not in text:
        offenders.append(f"{document}: does not state the enforced {match.group(1)}% floor")
    if "coverage-floors.json" not in text:
        offenders.append(f"{document}: does not name coverage-floors.json")
    return offenders


def _release_offenders(text: str) -> list[str]:
    probe = _SMOKE_VERSION.search(_read("scripts/release/image.py"))
    assert probe, "release smoke probe changed; re-derive this gate"
    offenders: list[str] = []
    if probe.group(0) not in text:
        offenders.append("docs/RELEASE.md: does not name the live smoke version probe")
    for number, raw in enumerate(text.splitlines(), start=1):
        if re.search(r"crawlutron\.__version__[^|\n]{0,80}equals", raw, re.I):
            offenders.append(f"docs/RELEASE.md:{number}: claims __version__ is the root-version probe")
    return offenders


@lru_cache(maxsize=1)
def _expected_module_graph() -> str:
    modules, _importers = module_graph.build_graph()
    return f"```mermaid\n{module_graph.mermaid(modules)}\n```\n"


def _graph_offenders(text: str) -> list[str]:
    if text != _expected_module_graph():
        return ["docs/module-graph.md: generated graph differs from the live import graph"]
    return []


def _extraction_list_offenders(text: str) -> list[str]:
    match = re.search(r"supported_extractions`?\s*=\s*`\[([^\]]+)\]`", text)
    expected = [method.value for method in CrawlExtractionMethod]
    if match is None:
        return ["manual plan: supported_extractions assertion is missing"]
    listed = [item.strip() for item in match.group(1).split(",")]
    if listed != expected:
        return [f"manual plan: supported_extractions {listed} != live {expected}"]
    return []


def test_active_docs_name_no_removed_extraction_library():
    failures = [f"{name!r} is back in uv.lock" for name in _removed_from_lock()]
    failures.extend(
        hit for relative in _ACTIVE_DOCS for hit in _stale_extraction_offenders(_read(relative), relative)
    )
    assert not failures, "; ".join(failures)


def test_active_docs_gate_bites_a_reintroduced_library_claim():
    planted = "`full` — Unified pipeline (markitdown for documents)."
    offenders = _stale_extraction_offenders(planted, "README.md")
    assert any("names removed extraction library" in hit for hit in offenders), offenders


def test_discovery_descriptions_name_no_removed_extraction_library():
    assert not (failures := _description_offenders(dict(EXTRACTION_DESCRIPTIONS))), "; ".join(failures)


def test_discovery_descriptions_gate_bites_a_planted_claim():
    planted = {**dict(EXTRACTION_DESCRIPTIONS), "full": "markitdown for documents"}
    assert any("names removed library 'markitdown'" in hit for hit in _description_offenders(planted))


def test_extraction_source_names_no_removed_library():
    assert not (failures := _production_offenders()), "; ".join(failures)


def test_coverage_claims_match_the_enforced_floor():
    failures = [
        hit
        for relative in ("README.md", "docs/architecture-governance.md")
        for hit in _coverage_offenders(_read(relative), relative)
    ]
    assert not failures, "; ".join(failures)


def test_coverage_gate_bites_a_stale_no_badge_refusal():
    planted = "No coverage badge or coverage ratchet is claimed. No threshold, no fail-under."
    offenders = _coverage_offenders(planted, "docs/architecture-governance.md")
    assert any("denies the live codecov badge" in hit for hit in offenders), offenders


def test_release_smoke_names_the_live_version_probe():
    assert not (failures := _release_offenders(_read("docs/RELEASE.md"))), "; ".join(failures)


def test_release_smoke_gate_bites_a_false_version_claim():
    planted = "asserting the baked `crawlutron.__version__` equals the root version"
    assert any("claims __version__ is the root-version probe" in hit for hit in _release_offenders(planted))


def test_module_graph_matches_the_generated_import_graph():
    assert not (failures := _graph_offenders(_read("docs/module-graph.md"))), "; ".join(failures)


def test_module_graph_gate_bites_a_stale_edge():
    planted = _expected_module_graph().replace("```mermaid\n", "```mermaid\n    stale --> edge\n", 1)
    assert any("generated graph differs" in hit for hit in _graph_offenders(planted))


def test_manual_plan_extraction_list_matches_the_live_enum():
    plan = _read("docs/references/crawlutron-manual-test-plan.md")
    assert not (failures := _extraction_list_offenders(plan)), "; ".join(failures)


def test_manual_plan_extraction_list_gate_bites_a_planted_value():
    plan = _read("docs/references/crawlutron-manual-test-plan.md")
    planted = plan.replace("dom]", "dom, extra]", 1)
    assert any("!= live" in hit for hit in _extraction_list_offenders(planted))


def _deleted_text_offenders(text: str, document: str) -> list[str]:
    return [
        f"{document}:{number}: revived deleted extraction value {_DELETED_METHOD!r}"
        for number, raw in enumerate(text.splitlines(), start=1)
        if _DELETED_METHOD in raw.lower()
    ]


def _deleted_method_offenders() -> list[str]:
    offenders = [
        f"CrawlExtractionMethod: revived deleted value {method.value!r}"
        for method in CrawlExtractionMethod
        if _DELETED_METHOD in method.value.lower()
    ]
    offenders.extend(
        f"EXTRACTION_DESCRIPTIONS[{key}]: revived deleted value"
        for key, description in EXTRACTION_DESCRIPTIONS.items()
        if _DELETED_METHOD in key.lower() or _DELETED_METHOD in description.lower()
    )
    for relative in _LIVE_DELETED_METHOD_SOURCES + _ACTIVE_DOCS:
        offenders.extend(_deleted_text_offenders(_read(relative), relative))
    return offenders


def test_deleted_extraction_value_is_absent_from_live_surfaces():
    assert not (failures := _deleted_method_offenders()), "; ".join(failures)


def test_deleted_extraction_value_gate_bites_a_documented_value():
    planted = "- `trafilatura` — HTML-only main-text path; documents are not parsed."
    offenders = _deleted_text_offenders(planted, "README.md")
    assert any("revived deleted extraction value" in hit for hit in offenders), offenders


def test_deleted_extraction_value_gate_bites_a_source_value():
    planted = 'EXTRACTION_TRAFILATURA: str = "trafilatura"'
    offenders = _deleted_text_offenders(planted, "packages/core/src/crawlutron/vocabulary/extraction.py")
    assert any("revived deleted extraction value" in hit for hit in offenders), offenders
