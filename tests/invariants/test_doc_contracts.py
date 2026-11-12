"""Documents point at paths that exist; manifests set no dead knob."""

from __future__ import annotations

import ast
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from pydantic import BaseModel

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.json_value import JsonValue
from crawlutron.models.render_request import RenderRequest
from tests.chokepoint_walks import REPO_ROOT, parsed
from tests.invariants._env_knobs import _KNOB_PATTERN, _consumed_knobs
from tests.rules import Rule

_DOC_REFERENCE_EXCLUSIONS = {
    "CHANGELOG.md": "a record of what shipped; its citations name modules that were refactored away",
    # Renamed from CHANGELOG.md in #561; same nature -- a historical record
    # quoting the tree as it stood when each release shipped.
    "CHANGELOG-legacy.md": "a historical record quoting the tree it shipped",
    "docs/reviews/ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md": "a dated review quoting the tree it reviewed",
    "docs/review-and-refactoring-plan.md": "stamped ARCHIVED; describes a topology that no longer exists",
    "docs/2026-09-15-performance-review.md": "a dated performance review quoting the tree it measured",
}


_SKIP_DIRECTORIES = frozenset(
    {".git", ".venv", "work", ".hardstand", "node_modules", ".pytest_cache", "__pycache__"}
)


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
    # Tracked *.md only -- an untracked task brief (never committed) must not red the doc gate.
    all_docs = sorted(
        str(document.relative_to(REPO_ROOT))
        for document in REPO_ROOT.rglob("*.md")
        if not _SKIP_DIRECTORIES.intersection(document.relative_to(REPO_ROOT).parts)
    )
    if not (REPO_ROOT / ".git").exists():
        return all_docs
    lf = subprocess.run(
        [shutil.which("git") or "git", "-C", str(REPO_ROOT), "ls-files", "*.md"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [name for name in all_docs if name in set(lf)]


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
            if not candidate:
                # Double backticks (``word``) leave whitespace captures.
                continue
            if not _is_tree_path_claim(candidate):
                continue
            if not (root / candidate.split(":")[0]).exists():
                offenders.append(f"{name} points at `{candidate}`, which does not exist")
    return offenders


_DOTTED_APP_SYMBOL = re.compile(r"^crawlutron[\w./]+$")
_FILE_EXTENSIONS = frozenset(
    {".py", ".md", ".toml", ".json", ".yaml", ".yml", ".ini", ".cfg", ".txt", ".in", ".sh"}
)


def _is_app_symbol_reference(candidate: str) -> bool:
    """Whether a backticked token is a crawlutron module.symbol reference (not a file path)."""
    if not _DOTTED_APP_SYMBOL.match(candidate):
        return False
    if candidate.endswith("/"):
        return False
    last_segment = candidate.split("/")[-1]
    # A real file reference ends with a known source/doc extension; a symbol
    # reference (crawlutron/logging_config.configure_service_logging) does not.
    if any(last_segment.endswith(ext) for ext in _FILE_EXTENSIONS):
        return False
    return True


_BARE_IDENT = re.compile(r"^[_A-Za-z][\w]*$")

# Bare words used as prose, shell commands, or feature option keys -- never app symbols.
_NON_SYMBOL_WORDS = frozenset(
    {
        "git",
        "python3",
        "pip",
        "python",
        "docker",
        "curl",
        "bash",
        "sh",
        "make",
        "npm",
        "node",
        "main",
        "run",
        "test",
        "build",
        "clone",
        "enter",
        "source",
        "activate",
        "checkout",
        "branch",
        "merge",
        "commit",
        "push",
        "pull",
        "note",
        "code",
        "field",
        "value",
        "type",
        "port",
        "host",
        "http",
        "https",
        "venv",
        "PEP",
        "True",
        "False",
        "None",
        # Python keywords that look like identifiers but are never app symbols.
        "and",
        "as",
        "assert",
        "async",
        "await",
        "break",
        "class",
        "continue",
        "def",
        "del",
        "elif",
        "else",
        "except",
        "finally",
        "for",
        "from",
        "global",
        "if",
        "import",
        "in",
        "is",
        "lambda",
        "nonlocal",
        "not",
        "or",
        "pass",
        "raise",
        "return",
        "try",
        "while",
        "with",
        "yield",
    }
)


def _app_symbol_definitions(root: pathlib.Path = REPO_ROOT) -> dict[str, frozenset[str]]:
    """Every top-level name defined in the crawlutron package, mapped to the module files that define it.

    A bare backticked name in a doc can only be checked against this index: if no
    module defines it, the doc is citing something that doesn't exist as a crawlutron symbol.
    """
    definitions: dict[str, set[str]] = {}
    for path in sorted((root / "packages" / "core" / "crawlutron").rglob("*.py")):
        tree = parsed(path)
        relative = path.relative_to(root).as_posix()
        for node in tree.body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                definitions.setdefault(node.name, set()).add(relative)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        definitions.setdefault(target.id, set()).add(relative)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                definitions.setdefault(node.target.id, set()).add(relative)
    return {name: frozenset(modules) for name, modules in definitions.items()}


def _symbol_defined_in(tree: ast.Module, symbol: str) -> bool:
    """Whether ``symbol`` is defined at top level of the given module tree."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.name == symbol:
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == symbol for t in node.targets
        ):
            return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == symbol:
            return True
    return False


def _doc_symbol_offenders(
    root: pathlib.Path = REPO_ROOT,
    app_root: pathlib.Path = REPO_ROOT,
    documents: list[str] | None = None,
    symbols: dict[str, frozenset[str]] | None = None,
) -> list[str]:
    """Backticked crawlutron symbols in live docs must resolve to definitions."""
    if symbols is None:
        symbols = _app_symbol_definitions(app_root)
    docs = _live_documents() if documents is None else documents
    offenders: list[str] = []
    for name in docs:
        document = root / name
        text = document.read_text(encoding="utf-8")
        for span in re.findall(r"`([^`\n]+)`", text):
            candidate = span.strip()
            if not candidate:
                continue
            if _is_app_symbol_reference(candidate):
                offenders.extend(_check_dotted_symbol(name, candidate, app_root))
                continue
            if name != "CONTRIBUTING.md":
                continue
            offenders.extend(_check_bare_symbol(name, candidate, symbols))
    return offenders


def _check_dotted_symbol(name: str, candidate: str, app_root: pathlib.Path) -> list[str]:
    """Offenders for one dotted ``app...symbol`` reference."""
    parts = candidate.replace("/", ".").split(".")
    if len(parts) < 3:
        return []
    if _module_file(app_root, parts).exists():
        return []
    symbol = parts[-1]
    module_path = (
        app_root / "packages" / "core" / "crawlutron" / pathlib.Path(*parts[1:-1]).with_suffix(".py")
    )
    if not module_path.exists():
        return [f"{name}: `{candidate}` module does not exist"]
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    if not _symbol_defined_in(tree, symbol):
        return [f"{name}: `{candidate}` symbol not defined in {module_path.relative_to(app_root)}"]
    return []


def _module_file(app_root: pathlib.Path, parts: list[str]) -> pathlib.Path:
    """The citation read as a MODULE path: crawlutron/<pkg...>/<name>.py."""
    return (app_root / "packages" / "core" / "crawlutron").joinpath(*parts[1:]).with_suffix(".py")


def _check_bare_symbol(name: str, candidate: str, symbols: dict[str, frozenset[str]]) -> list[str]:
    """Offenders for one bare identifier in CONTRIBUTING.md."""
    if not _BARE_IDENT.match(candidate):
        return []
    if candidate.isupper() or candidate in _NON_SYMBOL_WORDS:
        return []
    defining = symbols.get(candidate, frozenset())
    if defining:
        return []
    return [f"{name}: `{candidate}` names no app symbol"]


DOC_SYMBOLS_RESOLVE_TO_DEFINITIONS = Rule(
    name="doc-symbols-resolve-to-definitions",
    find=_doc_symbol_offenders,
    summary=(
        "A backticked app symbol in a live doc must name something app/ actually defines, "
        "so a rename or deletion can't leave docs citing ghosts."
    ),
    # The detector reads doc FILES (root/documents/symbols), not a parsed tree, so no snippet can go
    # through check_bites' ast.parse -- its bite and clean halves are the tmp-copy tests below.
    proof_path=REPO_ROOT / "CONTRIBUTING.md",
    bite=lambda: (),
    clean=None,
)


_FENCED_JSON = re.compile(r"```json\n(.*?)```", re.DOTALL)


def _classify_request_shape(payload: dict[str, JsonValue]) -> type[BaseModel] | None:
    """The live model a JSON object claims to be, or None when it is not a request."""
    if not isinstance(payload, dict):
        return None
    if "strategies" in payload:
        return CrawlPolicy
    if "options" in payload and ("url" in payload or "urls" in payload):
        return RenderRequest
    if "url" in payload or "urls" in payload:
        return CrawlRequest
    return None


def _doc_example_offenders(root: pathlib.Path = REPO_ROOT, documents: list[str] | None = None) -> list[str]:
    """Every fenced request-payload example in a live doc must validate against its model.

    Blocks that do not parse or do not name a request shape are skipped.
    """
    offenders: list[str] = []
    for name in _live_documents() if documents is None else documents:
        document = root / name
        for index, block in enumerate(_FENCED_JSON.findall(document.read_text(encoding="utf-8")), start=1):
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                continue
            model = _classify_request_shape(payload)
            if model is None:
                continue
            try:
                model.model_validate(payload)
            except Exception as error:
                first = str(error).split("\n")[0]
                offenders.append(f"{name} json example #{index} is rejected by {model.__name__}: {first}")
    return offenders


def test_live_doc_json_request_examples_validate():
    assert not (failures := _doc_example_offenders()), "; ".join(failures)


def test_doc_example_gate_bites(tmp_path):
    """The gate must reject a request example the live model refuses."""
    assert _doc_example_offenders() == []
    (tmp_path / "doc_ghost.md").write_text(
        '```json\n{"url": "https://example.com/a", "request_profile": {}}\n```\n',
        encoding="utf-8",
    )
    offenders = _doc_example_offenders(root=tmp_path, documents=["doc_ghost.md"])
    assert len(offenders) == 1, (
        "a request example a model rejects must produce exactly one offender "
        f"(one doc, one fence): got {offenders}"
    )
    assert "rejected by CrawlRequest" in offenders[0], offenders


def test_doc_example_gate_spares_non_request_fences(tmp_path):
    """Response shapes and non-JSON fences are not requests and must not fire."""
    (tmp_path / "doc_ghost.md").write_text(
        '```json\n{"request_id": "a", "results": [{"url": "https://example.com/a"}]}\n```\n'
        "```\nnot json at all\n```\n"
        '```json\n{"url": "https://example.com/a"}\n```\n',
        encoding="utf-8",
    )
    assert _doc_example_offenders(root=tmp_path, documents=["doc_ghost.md"]) == []


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


def test_document_enumeration_uses_relative_not_absolute_parts(monkeypatch, tmp_path):
    """Relative-path enumeration survives a CI checkout rooted under ``work``/;

    a document.parts rewrite would skip every document there and go green for
    looking at zero files.
    """
    fake_root = tmp_path / "home" / "runner" / "work" / "crawlutron" / "crawlutron"
    fake_root.mkdir(parents=True)
    (fake_root / "README.md").write_text("# readme\n", encoding="utf-8")
    monkeypatch.setattr("tests.invariants.test_doc_contracts.REPO_ROOT", fake_root)
    docs = _markdown_documents()
    assert "README.md" in docs, f"README.md was skipped under a work/-prefixed root: {docs}"


def test_live_documents_are_derived_not_enumerated():
    """The gate on the gate: coverage must equal the tree.

    A new document must arrive covered -- the failure a hand-kept list cannot
    catch, when one audit sat outside the original nine with seven dead paths."""
    assert set(_live_documents()) | set(_DOC_REFERENCE_EXCLUSIONS) == set(_markdown_documents())
    assert not (stale := set(_DOC_REFERENCE_EXCLUSIONS) - set(_markdown_documents())), stale


def test_live_docs_point_at_paths_that_exist():
    assert not (failures := _broken_doc_reference_offenders()), "; ".join(failures)


def test_doc_symbols_resolve_to_definitions():
    """Live docs cite no app symbols that don't exist."""
    assert not (failures := _doc_symbol_offenders()), "; ".join(failures)


def test_doc_symbol_detector_bites(tmp_path):
    """The gate must catch a stale dotted path and a stale bare symbol.

    The rule's detector reads doc FILES, not a parsed tree, so its bite is a
    synthetic ghost citation in a copy, not a parseable snippet."""
    symbols = _app_symbol_definitions()
    assert _doc_symbol_offenders(symbols=symbols) == []
    (tmp_path / "CONTRIBUTING.md").write_text(
        "See ``crawlutron/ghost_module.configure_service_logging``.\nAnd ``_ghost_symbol`` is also gone.\n",
        encoding="utf-8",
    )
    offenders = _doc_symbol_offenders(root=tmp_path, documents=["CONTRIBUTING.md"], symbols=symbols)
    assert any("module does not exist" in hit for hit in offenders), offenders
    assert any("names no app symbol" in hit for hit in offenders), offenders
    (tmp_path / "app_transport_ghost.md").write_text(
        "See ``crawlutron/transport.robots_parse.no_such_symbol_xyz``.\n",
        encoding="utf-8",
    )
    offenders = _doc_symbol_offenders(root=tmp_path, documents=["app_transport_ghost.md"], symbols=symbols)
    assert any("symbol not defined in" in hit for hit in offenders), offenders


def test_doc_symbol_detector_spared_clean_references(tmp_path):
    """Real symbols and non-symbol bare words don't fire the gate."""
    symbols = _app_symbol_definitions()
    document = REPO_ROOT / "CONTRIBUTING.md"
    (tmp_path / "CONTRIBUTING.md").write_text(
        document.read_text(encoding="utf-8")
        + "\nSee ``crawlutron/models/host_policy.HostPolicy`` and ``filter_js_endpoints``.\n"
        + "And ``git``, ``main`` and ``PEP 8`` are not symbols.\n",
        encoding="utf-8",
    )
    assert _doc_symbol_offenders(root=tmp_path, documents=["CONTRIBUTING.md"], symbols=symbols) == []


def test_a_dotted_citation_naming_a_real_nested_module_passes(tmp_path):
    """crawlutron.<pkg>.<module> cites a module file, not a symbol in one. RED against the join bug:
    parts[1:-1] built <pkg>.py, refusing the true module crawlutron/config/settings.py (found by the #677
    reviewer's skill prose). The last dotted part may BE the module.
    """
    (tmp_path / "probe.md").write_text(
        "Build it via ``crawlutron/config/settings.CrawlerSettings``; module: "
        "``crawlutron/config/settings``.\n",
        encoding="utf-8",
    )
    assert _doc_symbol_offenders(root=tmp_path, documents=["probe.md"]) == []
