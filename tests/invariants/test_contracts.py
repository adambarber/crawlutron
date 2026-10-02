"""Declared surfaces: env readers, knobs, DDL, service roots, protocols, MCP tools.

A name a process advertises, a setting it reads, a table it migrates, a tool it
exposes. These are what an integrator sees, so a second definition is a lie
about the interface rather than an internal tidy-up."""

from __future__ import annotations

import ast
import pathlib
import re
from collections.abc import Iterable
from pathlib import Path

import pytest

from app.settings import ENVIRONMENT_FIELD_ALIASES
from scripts.ast_scopes import is_body_owner
from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    app_trees,
    assignment_targets,
    attribute_reads,
    call_nodes,
    calls_to_any,
    class_defs,
    file_tree,
    functions,
    lineno,
    name_reads,
    nodes_of,
    offender,
    offender_at,
    parent_map,
    relative_posix,
    resolved_call_name,
    string_literals,
    try_blocks,
    writes,
)
from tests.invariants._shared import (
    _declared_public_symbols,
    _enclosing_calls,
    _enclosing_nodes,
    _relative_app_path,
    assert_property_detector_bites,
)
from tests.property_registry import Property
from tests.rules import Bites, Rule, Silent


def _job_engine_payload_chokepoint_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    relative = relative_posix(path)
    has_builder = any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "_job_engine_payload"
        for node in nodes_of(tree)
    )
    if relative != "app/api/job_runner.py" and not has_builder:
        return []
    execute_names = {"_execute_crawl", "_real_execute_crawl"}
    builder_names = {"_job_engine_payload"}
    for node in writes(tree):
        value = node.value
        targets = assignment_targets(node)
        if isinstance(value, ast.Name) and value.id in execute_names:
            execute_names.update(target.id for target in targets if isinstance(target, ast.Name))
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in builder_names
        ):
            builder_names.update(target.id for target in targets if isinstance(target, ast.Name))
    offenders: list[str] = []
    for call in calls_to_any(tree, execute_names):
        payload = (
            call.args[0]
            if call.args
            else next(
                (keyword.value for keyword in call.keywords if keyword.arg in {"payload", "data"}), None
            )
        )
        if isinstance(payload, ast.Name) and payload.id in builder_names:
            continue
        if (
            isinstance(payload, ast.Call)
            and isinstance(payload.func, ast.Name)
            and payload.func.id in builder_names
        ):
            continue
        offenders.append(offender_at(path, node, "_execute_crawl payload bypasses _job_engine_payload"))
    return offenders


JOB_ENGINE_PAYLOAD_USES_SINGLE_BUILDER = Rule(
    name="job-engine-payload-uses-single-builder",
    find=_job_engine_payload_chokepoint_offenders,
    summary=(
        "Build whole-site engine payloads only with _job_engine_payload, "
        "so a new call site to _execute_crawl cannot silently skip the builder"
    ),
    proof_path=REPO_ROOT / "app" / "api" / "job_runner.py",
    bite=lambda: (
        Bites(
            "async def run(job, batch):\n"
            "    payload = {'urls': batch, 'policy': dict(job.policy)}\n"
            "    await _execute_crawl(payload)\n",
            "payload bypasses _job_engine_payload",
        ),
        # An alias of the executor is the same bypass spelled a second way.
        Bites(
            "async def run(job, batch):\n"
            "    execute = _execute_crawl\n"
            "    await execute({'urls': batch, 'policy': {}})\n",
            "payload bypasses _job_engine_payload",
        ),
    ),
    clean=lambda: (
        "async def run(job, batch):\n"
        "    payload = _job_engine_payload(job, batch)\n"
        "    await _execute_crawl(payload)\n",
    ),
)


def test_job_engine_payload_uses_single_builder():
    JOB_ENGINE_PAYLOAD_USES_SINGLE_BUILDER.scan()


def test_job_engine_payload_chokepoint_detector_bites():
    JOB_ENGINE_PAYLOAD_USES_SINGLE_BUILDER.check_bites()


# The strategy chain's raise-chokepoint. A strategy is a STEP in a ladder, so
# a step that raises must become a classified failed result and the ladder
# must move on; the sink is a direct call to a strategy runner, and the
# requirement is that it sits inside a try with an ``except Exception``.
# Tree-wide, because a second module may grow a strategy runner and reach for
# the chain's helpers.
_STRATEGY_RUNNERS = frozenset({"_run_http_strategy", "_run_browser_strategy"})


def _guarded_node_ids(tree: ast.AST) -> set[int]:
    """Identity of every node in a ``try`` body that has an ``except Exception``.

    Only the guarded ``body`` counts. A call in an ``except``/``else``/
    ``finally`` clause is not protected by its own try, so including those
    would open exactly the hole this detector closes.
    """
    guarded: set[int] = set()
    for node in try_blocks(tree):
        if not any(
            isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}
            for handler in node.handlers
        ):
            continue
        for statement in node.body:
            guarded.update(id(child) for child in nodes_of(statement))
    return guarded


def _runner_call_name(node: ast.Call) -> str:
    """A strategy runner's name, whether it was called bare or as a method.

    Resolving both spellings is what keeps the invariant attached to the CODE
    rather than to one particular refactor's shape. A detector that matched only
    ``ast.Name`` reported a clean tree at the exact moment the runner became a
    method -- the refactor that cannot weaken the invariant is the one most
    likely to make it silently unenforced, because nothing about the change looks
    like touching it.
    """
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _unguarded_strategy_runner_offenders(tree: ast.AST, path: pathlib.Path) -> list[str]:
    guarded = _guarded_node_ids(tree)
    offenders: list[str] = []
    for node in call_nodes(tree):
        name = _runner_call_name(node)
        if name in _STRATEGY_RUNNERS and id(node) not in guarded:
            offenders.append(offender_at(path, node, f"{name} invoked outside a try/except Exception"))
    return offenders


STRATEGY_RUNNERS_ARE_CALLED_UNDER_A_GUARD = Rule(
    name="strategy-runners-are-called-under-a-guard",
    find=_unguarded_strategy_runner_offenders,
    summary=(
        "A strategy that raises must become a classified failed step that the ladder "
        "moves on from, not an exception that escapes the chain; call each runner "
        "inside a try with an except Exception"
    ),
    proof_path=REPO_ROOT / "app" / "crawl" / "strategy_chain.py",
    bite=lambda: (
        # Both runners called bare, outside any try: each unguarded call is a finding.
        Bites(
            "async def _execute_strategy(context, strategy):\n"
            "    if strategy in HTTP_STRATEGIES:\n"
            "        return await _run_http_strategy(context, strategy)\n"
            "    return await _run_browser_strategy(context, strategy)\n",
            "invoked outside a try/except Exception",
            count=2,
        ),
        # A narrow handler is not a guard: a bare except TypeError lets every
        # other raise through, which is the shape the bug shipped as.
        Bites(
            "async def _execute_strategy(context, strategy):\n"
            "    try:\n"
            "        return await _run_http_strategy(context, strategy)\n"
            "    except TypeError:\n"
            "        return None\n",
            "invoked outside a try/except Exception",
        ),
        # The runner reached as a METHOD is the same sink. A detector that matched
        # only a bare name goes blind at the moment the runner becomes a method --
        # which is a refactor that looks like it cannot weaken the invariant.
        Bites(
            "class Run:\n"
            "    async def execute(self, strategy):\n"
            "        return await self._run_browser_strategy(strategy)\n",
            "invoked outside a try/except Exception",
        ),
    ),
    clean=lambda: (
        # The guarded form parses and is spared.
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    except Exception as error:\n"
        "        return engine_result_from_exception(error=error)\n",
        # The guard is satisfied by an enclosing try too, not only a sibling one.
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        if strategy:\n"
        "            return await _run_browser_strategy(context, strategy)\n"
        "    except Exception:\n"
        "        return None\n",
        # A method form under the guard stays clean.
        "class Run:\n"
        "    async def execute(self, strategy):\n"
        "        try:\n"
        "            return await self._run_browser_strategy(strategy)\n"
        "        except Exception as error:\n"
        "            return engine_result_from_exception(error=error)\n",
    ),
)


def test_every_strategy_runner_call_is_guarded():
    STRATEGY_RUNNERS_ARE_CALLED_UNDER_A_GUARD.scan()


def test_strategy_runner_guard_detector_bites():
    STRATEGY_RUNNERS_ARE_CALLED_UNDER_A_GUARD.check_bites()


_KNOB_PATTERN = re.compile(r"\bCRAWLUTRON_[A-Z0-9_]+\b")


def _documented_browser_knobs() -> set[str] | None:
    env_path = REPO_ROOT / ".env.example"
    readme_path = REPO_ROOT / "README.md"
    if not env_path.exists() or not readme_path.exists():
        return None
    env_names = {
        match.group(0)
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
        for match in _KNOB_PATTERN.finditer(line)
    }
    readme_names = set(_KNOB_PATTERN.findall(readme_path.read_text(encoding="utf-8")))
    return {name for name in env_names | readme_names if not name.endswith("_")}


def _settings_alias_declaration_constant(
    path: pathlib.Path,
    tree: ast.AST,
    node: ast.Constant,
) -> bool:
    if _relative_app_path(path) != "app/settings.py":
        return False
    parents = parent_map(tree)
    current: ast.AST = node
    while id(current) in parents:
        current = parents[id(current)]
        if isinstance(current, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ENVIRONMENT_FIELD_ALIASES"
            for target in current.targets
        ):
            return True
    return False


_ENV_READER_NAMES = frozenset(
    {
        "get",
        "getenv",
        "get_env_float",
        "get_env_int",
        "get_mapping_float",
        "get_mapping_int",
        "parse_bool_env",
        "parse_bool_value",
    }
)


def _environment_reader_reference(
    tree: ast.AST,
    node: ast.Constant,
    field_environments: dict[str, str],
) -> str | None:
    parents = parent_map(tree)
    expression = parents.get(id(node))
    scope = parents.get(id(expression)) if expression is not None else None
    if (
        isinstance(node, ast.Constant)
        and isinstance(expression, ast.Expr)
        and expression.value is node
        # The block-owning statement kinds, named rather than ``ast.stmt``: "is
        # this the first statement of a block" asks for a body, and only some
        # statements have one. ``ast.stmt`` says none of them do, so the check
        # had to be written against a type that could not answer it.
        and scope is not None
        and is_body_owner(scope)
        and scope.body
        and scope.body[0] is expression
    ):
        return None
    current: ast.AST = node
    alias_value: str | None = None
    is_alias_lookup = False
    reaches_reader = False
    while id(current) in parents:
        current = parents[id(current)]
        if (
            isinstance(current, ast.Subscript)
            and isinstance(current.value, ast.Name)
            and current.value.id == "ENVIRONMENT_FIELD_ALIASES"
        ):
            is_alias_lookup = True
            if isinstance(current.slice, ast.Constant) and isinstance(current.slice.value, str):
                alias_value = field_environments.get(current.slice.value)
        if (
            isinstance(current, ast.Call)
            and resolved_call_name(tree, current).rsplit(".", 1)[-1] in _ENV_READER_NAMES
        ):
            reaches_reader = True
    if not reaches_reader:
        return None
    if is_alias_lookup:
        return alias_value
    # ast.Constant.value is the full literal union, so a prefix test is a claim
    # about the value being a str, not about the node being a Constant.
    if isinstance(node.value, str) and node.value.startswith("CRAWLUTRON_"):
        return node.value
    return None


def _settings_fields() -> set[str]:
    tree = file_tree("app/settings.py")[1]
    for node in class_defs(tree):
        if node.name == "CrawlerSettings":
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    return set()


def _settings_alias_failures(
    aliases: dict[str, str],
    fields: set[str] | None = None,
) -> list[str]:
    known_fields = fields if fields is not None else _settings_fields()
    failures = [
        f"{field} maps to invalid environment name {environment}"
        for field, environment in aliases.items()
        if field not in known_fields or not environment.startswith("CRAWLUTRON_")
    ]
    fields_by_environment: dict[str, list[str]] = {}
    for field, environment in aliases.items():
        fields_by_environment.setdefault(environment, []).append(field)
    failures.extend(
        f"{', '.join(sorted(fields_for_environment))} map to {environment}"
        for environment, fields_for_environment in sorted(fields_by_environment.items())
        if len(fields_for_environment) > 1
    )
    return failures


def _production_browser_knob_references() -> set[str]:
    references: set[str] = set()
    fields = _settings_fields()
    field_knobs = {f"CRAWLUTRON_{field.upper()}": field for field in fields}
    for path, tree in app_trees(exclude=frozenset()):
        for node in string_literals(tree):
            if isinstance(node.value, str) and (not _settings_alias_declaration_constant(path, tree, node)):
                environment = _environment_reader_reference(
                    tree,
                    node,
                    ENVIRONMENT_FIELD_ALIASES,
                )
                if environment is not None:
                    references.add(environment)
            if isinstance(node, ast.Attribute) and _relative_app_path(path) != "app/settings.py":
                environment = ENVIRONMENT_FIELD_ALIASES.get(node.attr)
                if environment is not None:
                    references.add(environment)
                conventional = f"CRAWLUTRON_{node.attr.upper()}"
                if conventional in field_knobs:
                    references.add(conventional)
    dockerfile = REPO_ROOT / "Dockerfile"
    if dockerfile.exists():
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if line.lstrip().startswith(("CMD", "ENTRYPOINT")):
                references.update(_KNOB_PATTERN.findall(line))
    return references


def _documented_knob_offenders(
    documented: set[str] | None,
    references: set[str],
) -> list[str]:
    if documented is None:
        return ["documented knob source could not be located"]
    if not documented:
        return ["no documented CRAWLUTRON knobs were found"]
    return [f"{name} has no production consumer" for name in sorted(documented - references)]


def _resolved_env_reader_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    references: set[str] = set()
    for node in string_literals(tree):
        if isinstance(node.value, str):
            environment = _environment_reader_reference(tree, node, ENVIRONMENT_FIELD_ALIASES)
            if environment is not None:
                references.add(environment)
    return _documented_knob_offenders(
        {"CRAWLUTRON_LAZY_SCROLL_STEPS"},
        references,
    )


def _consumed_knobs() -> set[str]:
    """Every ``CRAWLUTRON_*`` name app/ actually reads from the environment.

    ``_environment_reader_reference`` is the same detector the forward
    direction uses, so the two agree by construction rather than by two
    hand-maintained lists happening to line up. It resolves both the literal
    form and the ``ENVIRONMENT_FIELD_ALIASES`` lookup, and it already declines
    strings that only appear in a docstring, which is what keeps a prose
    mention of a knob from reading as a consumer.
    """
    names: set[str] = set()
    for _path, tree in app_trees():
        for node in string_literals(tree):
            if isinstance(node.value, str):
                reference = _environment_reader_reference(tree, node, ENVIRONMENT_FIELD_ALIASES)
                if reference is not None:
                    names.add(reference)
    return names


def _undocumented_knob_offenders() -> list[str]:
    """The reverse of ``_documented_knob_offenders``.

    The forward direction guards against publishing a knob that does nothing.
    This one guards the failure the forward direction cannot see: a knob that
    works, that the code reads, and that an operator has no way to find. The
    read is real either way, so the cost is not a crash -- it is a setting
    nobody knows exists, discovered by reading the source, and set by editing
    a deploy manifest.
    """
    documented = _documented_browser_knobs()
    if documented is None:
        return ["documented knob source could not be located"]
    return [
        f"{name} is read from the environment but never documented"
        for name in sorted(_consumed_knobs() - documented)
    ]


def test_every_consumed_knob_is_documented():
    assert not _undocumented_knob_offenders(), "; ".join(_undocumented_knob_offenders())


def test_every_consumed_knob_is_documented_bites():
    """The reverse gate has to be able to fail, or it is decoration.

    Asserted against a real tree: this passes if and only if the walk finds a
    genuine consumer and the document set genuinely omits it, so deleting the
    walk cannot turn the test green.
    """
    consumed = _consumed_knobs()
    assert "CRAWLUTRON_HOST" in consumed, "the walk stopped finding real consumers"
    offenders = [
        f"{name} is read from the environment but never documented"
        for name in sorted(consumed - {"CRAWLUTRON_HOST"})
    ]
    assert len(offenders) == len(consumed) - 1
    assert _undocumented_knob_offenders() == []


def test_documented_browser_knobs_have_consumers():
    assert not _settings_alias_failures(ENVIRONMENT_FIELD_ALIASES)
    failures = _documented_knob_offenders(
        _documented_browser_knobs(),
        _production_browser_knob_references(),
    )
    assert not failures, "; ".join(failures)


_DOC_REFERENCE_EXCLUSIONS = {
    "CHANGELOG.md": "a record of what shipped; its citations name modules that were refactored away",
    "ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md": "a dated review quoting the tree it reviewed",
    "docs/review-and-refactoring-plan.md": "stamped ARCHIVED; describes a topology that no longer exists",
    "docs/typing-plan.md": "a snapshot audit whose file references predate a reorganisation, and says so",
}
_SKIP_DIRECTORIES = frozenset({".git", ".venv", "work", "node_modules", ".pytest_cache", "__pycache__"})

_BACKTICKED_PATH = re.compile(
    r"^[\w][\w./-]*/[\w./-]*\.(?:py|md|toml|json|ya?ml|ini|cfg|txt|in|sh)(?::\d+)?$"
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
        # Relative parts, never absolute ones: the skip list names directories
        # of THIS repository (`docs/work/`), and matching it against the
        # absolute path drops every document on any checkout that happens to
        # live under a directory with one of those names. GitHub Actions
        # checks out to /home/runner/work/<repo>/<repo>, so the `work` entry
        # alone excluded the entire tree and the doc gate passed by examining
        # nothing -- a gate that is green because it looked at zero files is
        # worse than one that is red, because it is silently disarmed.
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
    hand-kept list cannot catch. `docs/typing-plan.md` sat outside the
    original nine for exactly as long as it had seven dead paths, and
    nothing said so -- the exclusion set, not the covered set, is what a
    human maintains now.
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
        owner_module="app/settings.py",
        owner_symbols=("from_mapping",),
        definition_site="app/settings.py:from_mapping",
        fingerprints=("environment_reader_reference",),
        reason="A documented environment name must resolve through a value reader.",
    )
    assert_property_detector_bites(
        prop,
        _resolved_env_reader_offenders,
        caught={
            "mapping lookup passes field name to reader": (
                "from app.settings import ENVIRONMENT_FIELD_ALIASES\n"
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
            "from app.settings import ENVIRONMENT_FIELD_ALIASES\n"
            "def resolved(source):\n"
            "    return get_mapping_int(source, ENVIRONMENT_FIELD_ALIASES['browser_lazy_scroll_steps'], 3)\n"
        ),
    )


_MIGRATION_DDL_PROPERTY = Property(
    name="migration-ddl",
    owner_module="app/crawl/migrations/",
    owner_symbols=(),
    definition_site="app/crawl/migrations/",
    fingerprints=("ddl",),
    reason="Schema changes must remain ordered and reversible in the migration package.",
)


def _ddl_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    if _relative_app_path(path).startswith("app/crawl/migrations/"):
        return []
    found: list[str] = []
    for node in nodes_of(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if re.match(
            r"\s*(?:CREATE|ALTER|DROP)\s+(?:TABLE|INDEX|TRIGGER|VIEW)\b",
            node.value,
            re.IGNORECASE,
        ):
            found.append(offender_at(path, node, node.value.splitlines()[0]))
    return found


DDL_IS_CONFINED_TO_THE_MIGRATIONS_PACKAGE = Rule(
    name="ddl-is-confined-to-the-migrations-package",
    find=lambda tree, path: _ddl_offenders(tree, path, _MIGRATION_DDL_PROPERTY),
    summary=(
        "CREATE/ALTER/DROP statements belong only in app/crawl/migrations, where "
        "they are ordered and reversible; a DDL statement anywhere else is a "
        "schema change that ships without a migration"
    ),
    # The scan needs every module including the sanctioned wrappers, not the
    # default SANCTIONED exclusion.
    scope=lambda *, exclude: app_trees(exclude=frozenset()),
    bite=lambda: Bites(
        "def bypass(conn):\n"
        "    conn.execute('CREATE TABLE x (id TEXT)')\n"
        "    conn.execute('ALTER TABLE x ADD COLUMN y TEXT')\n"
        "    conn.execute('DROP TABLE x')\n",
        "CREATE TABLE",
        count=3,
    ),
    clean=lambda: (
        # The migrations package is where DDL is allowed, so this shape is spared
        # there and must fire at every other module.
        Silent(
            "def apply(conn):\n    conn.execute('CREATE TABLE x (id TEXT)')\n",
            at=REPO_ROOT / "app" / "crawl" / "migrations" / "0001_crawl_jobs_initial.py",
        ),
    ),
)


def test_ddl_is_confined_to_the_migrations_package():
    DDL_IS_CONFINED_TO_THE_MIGRATIONS_PACKAGE.scan()


def test_ddl_detector_bites():
    DDL_IS_CONFINED_TO_THE_MIGRATIONS_PACKAGE.check_bites()


# --- MCP tool request models --------------------------------------------------
# The MCP dispatcher used to hand each handler the raw ``arguments`` dict and
# let the handler validate, while the *advertised* inputSchema was a separate
# hand-written JSON schema. Two descriptions of one contract, neither derived
# from the other -- and the advertised one claimed additionalProperties: False
# for a request model that in fact ignores extras. Now the registration names
# the pydantic model, the schema is derived from it, and the dispatcher
# validates. This guard keeps all three attached to one another.
_MCP_TOOLS_MODULE = "app/mcp/tools.py"


def _mcp_unvalidated_tool_offenders(tree, path, *, tools_module):
    if path.name != tools_module:
        return
    for call in calls_to_any(tree, {"ToolSpec"}):
        named = {kw.arg for kw in call.keywords if kw.arg}
        if "request_model" not in named:
            yield offender_at(
                path,
                call,
                "ToolSpec registers a tool without a request_model; the inputSchema and the call's argument "
                "validation are both derived from it, so a tool without one has no validated contract",
            )
    # The dispatcher must be the one validating, not the handlers.
    for node in functions(tree):
        if not node.name.startswith("_handle_"):
            continue
        for inner in call_nodes(node):
            if isinstance(inner.func, ast.Attribute) and inner.func.attr == "model_json_schema":
                yield offender_at(
                    path,
                    inner,
                    f"{node.name}() still derives a schema itself; the "
                    f"schema belongs to ToolSpec.input_schema, derived from the request model",
                )


#: The module's own basename, compared by the detector to each swept file's
#: ``path.name``. The old scan passed the full repo-relative path, which never
#: matched a basename -- sweeping nothing and reporting green. Passing the
#: basename is what actually confines the rule to app/mcp/tools.py.
_MCP_TOOLS_BASENAME = "tools.py"


EVERY_MCP_TOOL_DECLARES_THE_MODEL_THAT_VALIDATES_ITS_ARGUMENTS = Rule(
    name="every-mcp-tool-declares-the-model-that-validates-its-arguments",
    find=lambda tree, path: _mcp_unvalidated_tool_offenders(tree, path, tools_module=_MCP_TOOLS_BASENAME),
    summary=(
        "The inputSchema and the call's argument validation are both derived from "
        "the pydantic request model, so a tool without one has no validated "
        "contract and a handler that derives its own schema bypasses the dispatcher"
    ),
    proof_path=REPO_ROOT / _MCP_TOOLS_MODULE,
    bite=lambda: (
        Bites(
            "def register():\n    return ToolSpec(\n        name='x',\n        description='d',\n"
            "        output_schema=None,\n        handler=h,\n    )\n",
            "no validated contract",
        ),
        # A handler that builds its own schema is the other half of the bypass.
        Bites(
            "async def _handle_x(payload, settings, shutdown_event):\n"
            "    schema = CrawlRequest.model_json_schema()\n"
            "    return schema\n",
            "still derives a schema itself",
        ),
    ),
)


def test_every_mcp_tool_declares_the_model_that_validates_its_arguments():
    EVERY_MCP_TOOL_DECLARES_THE_MODEL_THAT_VALIDATES_ITS_ARGUMENTS.scan()


def test_mcp_request_model_detector_bites():
    from app.mcp.tools import TOOL_SPECS

    EVERY_MCP_TOOL_DECLARES_THE_MODEL_THAT_VALIDATES_ITS_ARGUMENTS.check_bites()

    # And the live registry must satisfy the invariant the guard asserts.
    for spec in TOOL_SPECS:
        assert spec.request_model is not None, spec.name
        assert spec.input_schema["properties"] == {
            k: v
            for k, v in spec.request_model.model_json_schema().get("properties", {}).items()
            if k not in spec.unsupported_fields
        }, f"{spec.name} advertises a schema its request model does not describe"


_PROTOCOL_CONSTANTS_MODULE = "app/mcp/constants.py"


def _module_level_assignment_lines(tree: ast.Module, names: Iterable[str]) -> dict[str, int]:
    """Line of each named module-level assignment, for the offender label.

    Distinct from :func:`_declared_public_symbols`, which reads the *declaration*
    of the surface; this locates a name the surface already vouches for, so a
    reader of the failure is handed the line to delete rather than a name to go
    looking for.
    """
    wanted = set(names)
    lines: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                lines[target.id] = node.lineno
    return lines


def _unproduced_protocol_constants(
    owner_tree: ast.Module | None,
    trees: Iterable[tuple[Path, ast.Module]],
    *,
    owner: str = _PROTOCOL_CONSTANTS_MODULE,
) -> list[str]:
    """Declared protocol constants that no other module under ``app/`` reads.

    This surface is a set of *values other code can rely on*: a JSON-RPC code a
    caller can be sent, a ``_meta`` key the server looks for. A declared value
    nothing reads is not a spare part of that surface, it is a claim about it --
    and an untested error code is worse than an absent one precisely because it
    reads as coverage. Two sat in that state: ``MISSING_REQUIRED_CLIENT_CAPABILITY``,
    which nothing ever raised, and ``META_CLIENT_INFO``, which nothing ever read,
    because the 2026-07-28 spec requires only ``protocolVersion`` and
    ``clientCapabilities`` in ``_meta``.

    Reachability is the check rather than a reviewer's memory, because the
    registry is where a spec addition goes to be *recorded* and the recording is
    not the same act as implementing it. The reference sweep is the whole
    ``app/`` tree -- a constant read from a module this test never heard of is
    produced -- with the declaring module excluded from the reference side,
    because its own assignments would otherwise satisfy the check for every
    name in it, which is the defect spelled backwards.

    ``__all__`` rather than every module-level assignment, reusing
    :func:`_declared_public_symbols`, because ``__all__`` is the module's own
    statement of the surface it offers and the claim under test is about that
    surface. An unreadable declaration is itself a finding: a registry the sweep
    cannot read is a registry nothing holds.
    """
    if owner_tree is None:
        return [f"{owner}: the protocol registry module could not be read, so it cannot be swept"]
    declared = _declared_public_symbols(owner_tree)
    if declared is None:
        return [f"{owner}: the protocol registry is not one literal __all__, so it cannot be swept"]
    referenced: set[str] = set()
    for path, tree in trees:
        if relative_posix(path) == owner:
            continue
        referenced.update(node.id for node in name_reads(tree))
        referenced.update(node.attr for node in attribute_reads(tree))
    lines = _module_level_assignment_lines(owner_tree, declared)
    offenders: list[str] = []
    for name in sorted(declared):
        if name in referenced:
            continue
        detail = f"{name} is declared but nothing in app/ reads it"
        if name in lines:
            offenders.append(offender(REPO_ROOT / owner, lines[name], detail))
        else:
            offenders.append(f"{owner} {detail}")
    return offenders


def test_every_protocol_constant_is_produced():
    """app/mcp/constants.py declares only codes and keys the server can reach."""
    _, owner_tree = file_tree(_PROTOCOL_CONSTANTS_MODULE)
    offenders = _unproduced_protocol_constants(owner_tree, app_trees())
    assert not offenders, "protocol constants with no production reader: " + ", ".join(offenders)


def test_protocol_constant_production_detector_bites():
    """An unread constant fires; reading it by name, or as an attribute, stays clean."""
    owner = ast.parse(
        "HEADER_MISMATCH = -32020\n"
        "META_CLIENT_INFO = 'x'\n"
        "__all__ = ['HEADER_MISMATCH', 'META_CLIENT_INFO']\n"
    )
    elsewhere = APP_ROOT / "mcp" / "protocol.py"
    silent = _unproduced_protocol_constants(
        owner, [(elsewhere, ast.parse("raise ProtocolError(HEADER_MISMATCH)\n"))]
    )
    assert len(silent) == 1, silent
    assert "constants.py:2 META_CLIENT_INFO is declared but nothing in app/ reads it" in silent[0], silent
    both = _unproduced_protocol_constants(
        owner,
        [
            (
                elsewhere,
                ast.parse("def check(meta):\n    return meta, HEADER_MISMATCH, err.META_CLIENT_INFO\n"),
            )
        ],
    )
    assert not both, both
    # The vacuous direction: a registry the sweep cannot read is not a clean bill.
    # Two different reasons, two different messages -- a module that never parsed
    # and a module whose __all__ is computed were reported by the same sentence,
    # which told a reader to go looking for a computed __all__ that was not there.
    unreadable = ast.parse("__all__ = compute()\n")
    for source, reason in (
        (unreadable, "the protocol registry is not one literal __all__"),
        (None, "the protocol registry module could not be read"),
    ):
        offenders = _unproduced_protocol_constants(source, [(elsewhere, ast.parse(""))])
        assert len(offenders) == 1 and f"{_PROTOCOL_CONSTANTS_MODULE}: {reason}" in offenders[0], offenders


_CYCLE_GUARD_CALL = "_entered"


_CYCLE_GUARD_OWNER = "_build_guarded"


def _cycle_guard_offenders(
    tree: ast.AST, path: Path, *, call: str = _CYCLE_GUARD_CALL, owner: str = _CYCLE_GUARD_OWNER
) -> list[str]:
    """Calls to the cycle guard made from anywhere but the one function that owns it.

    ``_entered`` was single-sourced as a *function* and then inlined back at
    every use: three container branches each carried the two-step "descend, and
    give up if already on the path" sequence, so the guard existed in four
    places. The function's own docstring already claimed the dedup -- "One
    function rather than three copies of the four lines" -- which made the gap
    worse, not better: a reader checking that claim would have believed it.

    The hazard is what a *fourth* branch does. Adding a container kind means
    writing a new ``isinstance`` arm, and the guard is not something the type
    system requires there. Routing every arm through one helper is what makes
    the check structural instead of remembered, so the rule under test is
    positional rather than structural: the guard is consulted in exactly one
    function in ``app/``, and that function is the one the arms call.
    """
    return [
        offender(path, node.lineno, f"{call}() is called from {enclosing!r}, not {owner}()")
        for node, enclosing in _enclosing_calls(tree, call)
        if enclosing != owner
    ]


CYCLE_GUARD_IS_CONSULTED_IN_EXACTLY_ONE_PLACE = Rule(
    name="cycle-guard-is-consulted-in-exactly-one-place",
    find=_cycle_guard_offenders,
    summary=(
        "app/models/taxonomy.py consults the cycle guard in one function; a call from "
        "an arm re-inlines the 'descend, give up if already on the path' pair it "
        "was deduped away from, so route every arm through the owner"
    ),
    proof_path=REPO_ROOT / "app" / "models.py",
    bite=lambda: (
        Bites(
            "def arm(value, seen):\n    entered = _entered(value, seen)\n    return entered\n",
            "is called from 'arm', not _build_guarded()",
        ),
        Bites("entered = _entered(value, seen)\n", "is called from '<module>'"),
    ),
    clean=lambda: (
        f"def {_CYCLE_GUARD_OWNER}(value, seen, build):\n"
        "    entered = _entered(value, seen)\n"
        "    return build(value, entered)\n",
    ),
)


def test_the_cycle_guard_is_applied_in_exactly_one_place():
    """app/models/taxonomy.py consults the cycle guard in one function, not once per container arm."""
    CYCLE_GUARD_IS_CONSULTED_IN_EXACTLY_ONE_PLACE.scan()


def test_cycle_guard_detector_bites():
    """A guard call inlined at an arm fires; the same call in the owner stays clean."""
    CYCLE_GUARD_IS_CONSULTED_IN_EXACTLY_ONE_PLACE.check_bites()


def test_cycle_guard_detector_respects_the_owner_parameter():
    # An owner by another name is still a second place, so the rule is about being
    # the one sanctioned function rather than about a literal spelling. Stated as a
    # direct detector call because the Rule layer hands one owner to every proof.
    assert _cycle_guard_offenders(
        ast.parse("def other(value, seen):\n    return _entered(value, seen)\n"),
        REPO_ROOT / "app" / "models.py",
        owner="different_owner",
    ), "a guard call must fire when the sanctioned owner is renamed"


_META_REASON_OWNER = "_require_meta_key"


_META_WRONG_TYPE_FRAGMENT = "Invalid _meta key (must be "


def _meta_key_reason_offenders(tree: ast.AST, path: Path, *, owner: str = _META_REASON_OWNER) -> list[str]:
    """The wrong-type half of the ``_meta`` key rejection authored outside its owner.

    ``validate_meta`` checked two required ``_meta`` keys and, for each, spelled
    out the same two-part reason -- absent versus wrong type -- and raised the
    same error around it. The distinction is the caller's only way to learn it
    forgot a key rather than sent the wrong shape, so it is a contract, and a
    duplicated one can drift in both directions at once: the two arms can stop
    agreeing on wording, or one can stop distinguishing the two cases at all,
    with the suite green either way.

    Matching the *literal* rather than the shape is deliberate. A second arm that
    built the same sentence a different way -- an f-string split across lines, a
    helper, a new noun -- would be precisely the drift, and a test that recognised
    the shape would wave it through. The rule is that the sentence is authored
    once, in the function both keys check through.
    """

    def is_reason(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _META_WRONG_TYPE_FRAGMENT in node.value
        )

    return [
        offender(path, lineno(node), f"the _meta key reason is authored in {enclosing!r}, not {owner}()")
        for node, enclosing in _enclosing_nodes(tree, is_reason)
        if enclosing != owner
    ]


THE_META_KEY_REJECTION_REASON_IS_AUTHORED_ONCE = Rule(
    name="the-meta-key-rejection-reason-is-authored-once",
    find=_meta_key_reason_offenders,
    summary=(
        "the wrong-type _meta reason is a contract the caller reads to learn it "
        "forgot a key rather than sent the wrong shape, so it is authored once, in "
        "the function both keys check through"
    ),
    bite=lambda: (
        Bites(
            "def validate_meta(meta, request_id):\n"
            f'    reason = f"{{key}} "\n'
            f'    reason += "{_META_WRONG_TYPE_FRAGMENT}{{key}})"\n',
            "authored in 'validate_meta'",
        ),
        Bites(
            f'WHY = "{_META_WRONG_TYPE_FRAGMENT}protocolVersion)"\n',
            "authored in '<module>'",
        ),
    ),
    clean=lambda: (
        # The one place the sentence legitimately lives: the owner function both
        # keys check through.
        f"def {_META_REASON_OWNER}(meta, key, expected, label, request_id):\n"
        f'    return f"{_META_WRONG_TYPE_FRAGMENT}{{label}}): {{key}})"\n',
        # Unrelated reasons are not the contract, so they must not be swept up
        # with it.
        'def validate_meta(meta):\n    raise ProtocolValidationError("Missing _meta block")\n',
        'def validate_meta(meta):\n    reason = f"Missing required _meta key: {key}"\n',
    ),
    proof_path=REPO_ROOT / "app" / "mcp" / "protocol.py",
)


def test_the_meta_key_rejection_reason_is_authored_once():
    """app/mcp/protocol.py spells the wrong-type _meta reason in one function."""
    THE_META_KEY_REJECTION_REASON_IS_AUTHORED_ONCE.scan()


def test_meta_key_reason_detector_bites():
    """A second authoring of the sentence fires; the one in the owner stays clean."""
    THE_META_KEY_REJECTION_REASON_IS_AUTHORED_ONCE.check_bites()
