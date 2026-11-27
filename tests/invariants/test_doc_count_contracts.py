"""README count and tool-list claims stay derived from the live enum and registry."""

from __future__ import annotations

import pathlib
import re

from crawlutron.vocabulary.taxonomy import CrawlClassification
from crawlutron_mcp.registry import TOOL_SPECS
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
