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

from app.settings import ENVIRONMENT_FIELD_ALIASES
from tests.chokepoint_walks import (
    APP_ROOT,
    REPO_ROOT,
    SANCTIONED,
    app_offenders,
    app_trees,
    assert_detector,
    attribute_reads,
    call_nodes,
    calls_to_any,
    class_defs,
    file_tree,
    functions,
    is_body_owner,
    name_reads,
    nodes_of,
    offender,
    offender_at,
    relative_posix,
    resolved_call_name,
    string_literals,
    try_blocks,
    writes,
)
from tests.invariants._shared import (
    _declared_public_symbols,
    _enclosing_calls,
    _relative_app_path,
    assert_property_detector_bites,
    assert_trees_clean,
)
from tests.property_registry import Property


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
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
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


def test_job_engine_payload_uses_single_builder():
    assert_trees_clean(
        _job_engine_payload_chokepoint_offenders,
        "Build whole-site engine payloads only with _job_engine_payload",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_job_engine_payload_chokepoint_detector_bites():
    pre_fix = ast.parse(
        "async def run(job, batch):\n"
        "    payload = {'urls': batch, 'policy': dict(job.policy)}\n"
        "    await _execute_crawl(payload)\n"
    )
    alias = ast.parse(
        "async def run(job, batch):\n"
        "    execute = _execute_crawl\n"
        "    await execute({'urls': batch, 'policy': {}})\n"
    )
    path = REPO_ROOT / "app" / "api" / "job_runner.py"
    assert _job_engine_payload_chokepoint_offenders(pre_fix, path)
    assert _job_engine_payload_chokepoint_offenders(alias, path)
    good = ast.parse(
        "async def run(job, batch):\n"
        "    payload = _job_engine_payload(job, batch)\n"
        "    await _execute_crawl(payload)\n"
    )
    assert not _job_engine_payload_chokepoint_offenders(good, path)


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


def test_every_strategy_runner_call_is_guarded():
    assert_trees_clean(
        _unguarded_strategy_runner_offenders,
        "A strategy that raises must become a failed step, not escape the chain",
        trees=app_trees(exclude=SANCTIONED),
    )


def test_strategy_runner_guard_detector_bites():
    path = REPO_ROOT / "app" / "crawl" / "strategy_chain.py"
    pre_fix = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    if strategy in HTTP_STRATEGIES:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    return await _run_browser_strategy(context, strategy)\n"
    )
    assert _unguarded_strategy_runner_offenders(pre_fix, path)
    # A narrow handler is not a guard: a bare ``except TypeError`` lets every
    # other raise through, which is the shape the bug shipped as.
    narrow = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    except TypeError:\n"
        "        return None\n"
    )
    assert _unguarded_strategy_runner_offenders(narrow, path)
    good = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        return await _run_http_strategy(context, strategy)\n"
        "    except Exception as error:\n"
        "        return _engine_result_from_exception(error=error)\n"
    )
    assert not _unguarded_strategy_runner_offenders(good, path)
    # The guard is satisfied by an enclosing try too, not only a sibling one.
    enclosing = ast.parse(
        "async def _execute_strategy(context, strategy):\n"
        "    try:\n"
        "        if strategy:\n"
        "            return await _run_browser_strategy(context, strategy)\n"
        "    except Exception:\n"
        "        return None\n"
    )
    assert not _unguarded_strategy_runner_offenders(enclosing, path)
    # The runner reached as a METHOD is the same sink. A detector that matched
    # only a bare name goes blind at the moment the runner becomes a method --
    # which is a refactor that looks like it cannot weaken the invariant, and is
    # therefore the one most likely to make it silently unenforced.
    method_unguarded = ast.parse(
        "class Run:\n"
        "    async def execute(self, strategy):\n"
        "        return await self._run_browser_strategy(strategy)\n"
    )
    assert _unguarded_strategy_runner_offenders(method_unguarded, path)
    method_guarded = ast.parse(
        "class Run:\n"
        "    async def execute(self, strategy):\n"
        "        try:\n"
        "            return await self._run_browser_strategy(strategy)\n"
        "        except Exception as error:\n"
        "            return _engine_result_from_exception(error=error)\n"
    )
    assert not _unguarded_strategy_runner_offenders(method_guarded, path)


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
    parents = {id(child): parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
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
    parents = {id(child): parent for parent in nodes_of(tree) for child in ast.iter_child_nodes(parent)}
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


_LIVE_NAVIGATIONAL_DOCS = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "docs/property-inventory.md",
    "docs/module-graph.md",
    "docs/references/crawlutron-agent-contract.md",
    "docs/references/crawlutron-telemetry.md",
    "docs/references/crawlutron-manual-test-plan.md",
)

_BACKTICKED_PATH = re.compile(
    r"^[\w][\w./-]*/[\w./-]*\.(?:py|md|toml|json|ya?ml|ini|cfg|txt|in|sh)(?::\d+)?$"
)


def _broken_doc_reference_offenders() -> list[str]:
    """Every repo path a live document tells a reader to open must exist.

    The scope is the point, and it is deliberately narrow. A record of what
    was true legitimately cites files that have since been deleted -- the
    CHANGELOG names modules that were refactored away, an archived plan
    describes a topology that no longer exists, and a dated review quotes the
    tree it reviewed. Rewriting those citations would falsify the record, so
    they are out of scope by design rather than by omission.

    What is in scope is the set of documents a reader arrives with intent to
    follow. A dead path there is not a historical artifact; it is a
    navigational dead end, and it has no other signal: prose still reads
    correctly, the build is green, and the reader only discovers it after
    deciding to trust the document.

    The span must contain a separator, and that is not a stylistic filter. A
    bare `robots.txt` or `browser.py` in prose is a basename being discussed,
    not a path being pointed at; only a slash makes the span a claim about
    this tree's layout.
    """
    offenders: list[str] = []
    for name in _LIVE_NAVIGATIONAL_DOCS:
        document = REPO_ROOT / name
        if not document.exists():
            offenders.append(f"{name} is listed as live but does not exist")
            continue
        for span in re.findall(r"`([^`\n]+)`", document.read_text(encoding="utf-8")):
            candidate = span.strip()
            if not _BACKTICKED_PATH.match(candidate):
                continue
            if not (REPO_ROOT / candidate.split(":")[0]).exists():
                offenders.append(f"{name} points at `{candidate}`, which does not exist")
    return offenders


def test_live_docs_point_at_paths_that_exist():
    assert not (failures := _broken_doc_reference_offenders()), "; ".join(failures)


def test_live_doc_reference_detector_bites():
    """The gate must be able to fail against the real documents, or it is
    decoration rather than a check. Proved by mutating the document set, not
    by feeding the helper a synthetic string."""
    current = _broken_doc_reference_offenders()
    assert current == [], current

    missing = REPO_ROOT / "README.md"
    original = missing.read_text(encoding="utf-8")
    try:
        missing.write_text(
            original + "\n`app/does_not_exist.py`\n", encoding="utf-8"
        )
        assert _broken_doc_reference_offenders() == [
            "README.md points at `app/does_not_exist.py`, which does not exist"
        ]
    finally:
        missing.write_text(original, encoding="utf-8")
    assert _broken_doc_reference_offenders() == []


def test_documented_knob_detector_bites():
    assert _documented_knob_offenders({"CRAWLUTRON_FAKE"}, set()) == [
        "CRAWLUTRON_FAKE has no production consumer"
    ]
    assert _documented_knob_offenders(None, set()) == ["documented knob source could not be located"]
    current = _documented_browser_knobs()
    assert current is not None
    assert "CRAWLUTRON_ALLOWED_HOSTS" in current
    assert not _documented_knob_offenders(
        {"CRAWLUTRON_ALLOWED_HOSTS"}, {"CRAWLUTRON_ALLOWED_HOSTS"}
    )
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


def test_ddl_is_confined_to_the_migrations_package():
    offenders = [
        found
        for path, tree in app_trees(exclude=frozenset())
        for found in _ddl_offenders(tree, path, _MIGRATION_DDL_PROPERTY)
    ]
    assert not offenders, "DDL outside app/crawl/migrations: " + ", ".join(offenders)


def test_ddl_detector_bites():
    assert_property_detector_bites(
        _MIGRATION_DDL_PROPERTY,
        _ddl_offenders,
        caught={
            "create alter drop": (
                "def bypass(conn):\n"
                "    conn.execute('CREATE TABLE x (id TEXT)')\n"
                "    conn.execute('ALTER TABLE x ADD COLUMN y TEXT')\n"
                "    conn.execute('DROP TABLE x')\n",
                3,
            )
        },
        quiet="def apply(conn):\n    conn.execute('CREATE TABLE x (id TEXT)')\n",
        quiet_path=REPO_ROOT / "app" / "crawl" / "migrations" / "0001_crawl_jobs_initial.py",
    )


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
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "ToolSpec":
            continue
        named = {kw.arg for kw in node.keywords if kw.arg}
        if "request_model" not in named:
            yield offender_at(
                path,
                node,
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


def test_every_mcp_tool_declares_the_model_that_validates_its_arguments():
    offenders = app_offenders(_mcp_unvalidated_tool_offenders, tools_module=_MCP_TOOLS_MODULE)
    assert not offenders, "an MCP tool is missing its validated contract: " + ", ".join(offenders)


def test_mcp_request_model_detector_bites():
    from app.mcp.tools import TOOL_SPECS

    missing = ast.parse(
        "def register():\n    return ToolSpec(\n        name='x',\n        description='d',\n"
        "        output_schema=None,\n        handler=h,\n    )\n"
    )
    offenders = list(
        _mcp_unvalidated_tool_offenders(
            missing, REPO_ROOT / "crawl" / "synthetic.py", tools_module="synthetic.py"
        )
    )
    assert offenders, "a ToolSpec without a request_model escaped the sweep"
    # A handler that builds its own schema is the other half of the bypass.
    self_scheming = ast.parse(
        "async def _handle_x(payload, settings, shutdown_event):\n"
        "    schema = CrawlRequest.model_json_schema()\n"
        "    return schema\n"
    )
    assert list(
        _mcp_unvalidated_tool_offenders(self_scheming, REPO_ROOT / _MCP_TOOLS_MODULE, tools_module="tools.py")
    ), "a handler deriving its own schema escaped the sweep"
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
    silent = _unproduced_protocol_constants(owner, [(elsewhere,
    ast.parse("raise ProtocolError(HEADER_MISMATCH)\n"))])
    assert len(silent) == 1, silent
    assert "constants.py:2 META_CLIENT_INFO is declared but nothing in app/ reads it" in silent[0], silent
    both = _unproduced_protocol_constants(
        owner,
        [(elsewhere,
        ast.parse("def check(meta):\n    return meta, HEADER_MISMATCH, err.META_CLIENT_INFO\n"))],
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
        assert len(offenders) == 1 and f"{_PROTOCOL_CONSTANTS_MODULE}: {reason}" in offenders[0], (
            offenders
        )


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


def test_the_cycle_guard_is_applied_in_exactly_one_place():
    """app/models.py consults the cycle guard in one function, not once per container arm."""
    offenders = app_offenders(_cycle_guard_offenders)
    assert not offenders, "the cycle guard is applied outside its single owner: " + ", ".join(offenders)


def test_cycle_guard_detector_bites():
    """A guard call inlined at an arm fires; the same call in the owner stays clean."""
    assert_detector(
        _cycle_guard_offenders,
        path=REPO_ROOT / "app" / "models.py",
        fires={
            "def arm(value, seen):\n"
            "    entered = _entered(value, seen)\n"
            "    return entered\n": "is called from 'arm', not _build_guarded()",
            "entered = _entered(value, seen)\n": "is called from '<module>'",
        },
        clean=(
            f"def {_CYCLE_GUARD_OWNER}(value, seen, build):\n"
            "    entered = _entered(value, seen)\n"
            "    return build(value, entered)\n",
        ),
    )
    # An owner by another name is still a second place, so the rule is about
    # being the one sanctioned function rather than about a literal spelling.
    assert _cycle_guard_offenders(
        ast.parse("def other(value, seen):\n    return _entered(value, seen)\n"),
        REPO_ROOT / "app" / "models.py",
        owner="different_owner",
    ), "a guard call must fire when the sanctioned owner is renamed"
