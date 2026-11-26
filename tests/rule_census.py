"""Census of invariant execution idioms by owner ruling #1474.

Class A = Rule without Registry (must be zero); B = neither; C = file-reading detectors.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_paths import REPO_ROOT, relative_posix
from tests.chokepoint_walks import trees_under

_INVARIANTS_DIR = REPO_ROOT / "tests" / "invariants"

#: Class B: plain config/runtime assertions with no detector, admitted by #1474.
#: Named, down only: new modules need a row; migrated ones drop with the ceiling.
PLAIN_ASSERTION_SET = frozenset(
    {
        "tests/invariants/test_anonymous_listing_isolation.py",
        "tests/invariants/test_api_service_shim_drain.py",
        "tests/invariants/test_ast_cache_isolation.py",
        "tests/invariants/test_atwin_store_executor.py",
        "tests/invariants/test_browser_cookie_host_scoping.py",
        "tests/invariants/test_canonical_tree_read_only.py",
        "tests/invariants/test_challenge_wait_poll.py",
        "tests/invariants/test_cli_flag_mapping.py",
        "tests/invariants/test_comment_length.py",
        "tests/invariants/test_cookie_load_read_only.py",
        "tests/invariants/test_cookie_namespace_predicate.py",
        "tests/invariants/test_coordination_cookies_begin_lock.py",
        "tests/invariants/test_core_declares_no_web_framework.py",
        "tests/invariants/test_coverage_floor_ratchet.py",
        "tests/invariants/test_detector_accounting.py",
        "tests/invariants/test_discovery_derived.py",
        "tests/invariants/test_doc_count_contracts.py",
        "tests/invariants/test_doc_drift_contract.py",
        "tests/invariants/test_doc_quickstart_contract.py",
        "tests/invariants/test_docstring_honesty.py",
        "tests/invariants/test_dynamic_regex.py",
        "tests/invariants/test_encoding_bounds.py",
        "tests/invariants/test_engine_import_seam.py",
        "tests/invariants/test_env_documentation.py",
        "tests/invariants/test_env_pool_bound.py",
        "tests/invariants/test_extract_leaf.py",
        "tests/invariants/test_facade_cli_table.py",
        "tests/invariants/test_facade_knob_parity.py",
        "tests/invariants/test_facade_sdk_mirror.py",
        "tests/invariants/test_file_size.py",
        "tests/invariants/test_gate_fanout_verdict_identity.py",
        "tests/invariants/test_gc_caller_law.py",
        "tests/invariants/test_handler_factory_collapse.py",
        "tests/invariants/test_harness_doubles.py",
        "tests/invariants/test_harness_middleware_parity.py",
        "tests/invariants/test_host_policy_entry_points.py",
        "tests/invariants/test_install_doc_contract.py",
        "tests/invariants/test_job_runner_reset_isolation.py",
        "tests/invariants/test_json_fallback_chokepoint.py",
        "tests/invariants/test_json_loads_chokepoint.py",
        "tests/invariants/test_knob_contracts.py",
        "tests/invariants/test_layer_exempt_ratchet.py",
        "tests/invariants/test_mcp_framework_seal.py",
        "tests/invariants/test_mcp_service_seal.py",
        "tests/invariants/test_moved_module_string_drift.py",
        "tests/invariants/test_mypy_scope.py",
        "tests/invariants/test_no_inline_suppressions.py",
        "tests/invariants/test_no_leaked_store_connections.py",
        "tests/invariants/test_no_unmediated_lifecycle.py",
        "tests/invariants/test_no_weighted_admission_survivors.py",
        "tests/invariants/test_numeric_knob_bounds.py",
        "tests/invariants/test_packaged_members_declare_only_existing_paths.py",
        "tests/invariants/test_parity_gate_reach.py",
        "tests/invariants/test_path_chokepoint.py",
        "tests/invariants/test_pin_registry_single_source.py",
        "tests/invariants/test_public_surface.py",
        "tests/invariants/test_registry.py",
        "tests/invariants/test_registry_bool_env.py",
        "tests/invariants/test_registry_cross_module.py",
        "tests/invariants/test_registry_robots.py",
        "tests/invariants/test_registry_targets.py",
        "tests/invariants/test_resource_lifecycle.py",
        "tests/invariants/test_rest_mcp_parity.py",
        "tests/invariants/test_route_layer.py",
        "tests/invariants/test_rule_arming.py",
        "tests/invariants/test_scan_roots.py",
        "tests/invariants/test_scan_scope_completeness.py",
        "tests/invariants/test_sdk_contract_pins.py",
        "tests/invariants/test_sdk_schema_shape.py",
        "tests/invariants/test_secret_redaction_single_homing.py",
        "tests/invariants/test_service_error_codes.py",
        "tests/invariants/test_service_framework_seal.py",
        "tests/invariants/test_spill_single_pass.py",
        "tests/invariants/test_subprocess_call_site_ratchet.py",
        "tests/invariants/test_suite_trees_uses_cache.py",
        "tests/invariants/test_syntax_floor_parse.py",
        "tests/invariants/test_taxonomy_construction.py",
        "tests/invariants/test_url_assertions_use_host_helper.py",
        "tests/invariants/test_walk_primitives_cache.py",
        "tests/invariants/test_walk_vacuity.py",
        "tests/invariants/test_warning_zero_ratchet.py",
        "tests/invariants/test_wire_mirror.py",
    }
)

#: Measured class-B census at #1474; down only, never raised to absorb growth.
PLAIN_ASSERTION_CEILING = 82

#: Class C: file-reading detector invariants (Rule without Registry), admitted by #1474.
#: Named, down only: a new one needs its relative path here and the ceiling lowered.
FILE_READING_DETECTOR_SET = frozenset({"tests/invariants/test_doc_contracts.py"})

#: Measured class-C census at #1474; down only, never raised to absorb growth.
FILE_READING_DETECTOR_CEILING = 1


def _bindings(tree: ast.Module) -> tuple[dict[str, set[str]], set[str]]:
    """Names bound to ``Rule``/``Registry`` by import, and any ``tests.rules`` alias."""
    classes: dict[str, set[str]] = {"Rule": set(), "Registry": set()}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "tests.rules":
            for alias in node.names:
                if alias.name in classes:
                    classes[alias.name].add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tests.rules":
                    modules.add(alias.asname or "tests")
    return classes, modules


def constructs(name: str, tree: ast.Module) -> bool:
    """True only when a CALL to a freshly imported ``name`` executes; imports don't."""
    classes, modules = _bindings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in classes[name]:
            return True
        if (
            isinstance(func, ast.Attribute)
            and func.attr == name
            and isinstance(func.value, ast.Name)
            and func.value.id in modules
        ):
            return True
    return False


def _modules(directory: pathlib.Path, has_rule: bool, has_registry: bool) -> list[pathlib.Path]:
    return sorted(
        path
        for path, tree in trees_under(directory)
        if path.name.startswith("test_")
        and constructs("Rule", tree) is has_rule
        and constructs("Registry", tree) is has_registry
    )


def _census_label(path: pathlib.Path) -> str:
    """Repo-relative label, or the bare name for a scratch-tree path."""
    try:
        return relative_posix(path)
    except ValueError:
        return path.name


def rule_only_modules(directory: pathlib.Path = _INVARIANTS_DIR) -> list[pathlib.Path]:
    """Rule-without-Registry modules: the class A + class C surface."""
    return _modules(directory, has_rule=True, has_registry=False)


def file_reading_detector_modules(directory: pathlib.Path = _INVARIANTS_DIR) -> list[pathlib.Path]:
    """Class C: file-reading detector invariants admitted by the named set."""
    return [path for path in rule_only_modules(directory) if _census_label(path) in FILE_READING_DETECTOR_SET]


def ast_scanning_modules(directory: pathlib.Path = _INVARIANTS_DIR) -> list[pathlib.Path]:
    """Class A: rule-only invariants not admitted as file-reading detectors."""
    return [
        path for path in rule_only_modules(directory) if _census_label(path) not in FILE_READING_DETECTOR_SET
    ]


def plain_assertion_modules(directory: pathlib.Path = _INVARIANTS_DIR) -> list[pathlib.Path]:
    """Class B: declares neither a Rule nor a Registry."""
    return _modules(directory, has_rule=False, has_registry=False)


def as_relative(paths: list[pathlib.Path]) -> list[str]:
    """Stable repo-relative spelling for a census result."""
    return sorted(relative_posix(path) for path in paths)
