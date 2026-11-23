"""Bites pinning codecov.yml structure to the live uv member set."""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

import pytest
import yaml

from tests._walk_primitives import import_roots, member_import_names
from tests.chokepoint_paths import REPO_ROOT

CONFIG = REPO_ROOT / "codecov.yml"
_ALLOWED_CARRYFORWARD = {"flag_management", "flags"}


class ComponentEntry(TypedDict):
    component_id: str
    paths: list[str]


class DefaultRules(TypedDict):
    carryforward: bool


class FlagManagement(TypedDict):
    default_rules: DefaultRules


class NightlyFlag(TypedDict):
    joined: bool


class Flags(TypedDict):
    nightly: NightlyFlag


class CodecovDoc(TypedDict):
    component_management: dict[str, object]
    flag_management: FlagManagement
    flags: Flags


def read() -> CodecovDoc:
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return cast(CodecovDoc, data)


def components(config: CodecovDoc) -> list[ComponentEntry]:
    entries = config["component_management"]["individual_components"]
    assert isinstance(entries, list), f"{CONFIG.name}:individual_components must be a list"
    return cast(list[ComponentEntry], entries)


def _carryforward_sites(node: object, trail: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(node, dict):
        own = [(*trail, "carryforward")] if "carryforward" in node else []
        nested = [
            site for key, value in node.items() for site in _carryforward_sites(value, (*trail, str(key)))
        ]
        return [*own, *nested]
    if isinstance(node, list):
        return [site for i, value in enumerate(node) for site in _carryforward_sites(value, (*trail, str(i)))]
    return []


def test_individual_components_is_a_list() -> None:
    assert isinstance(read()["component_management"]["individual_components"], list)


@pytest.mark.parametrize("key", ["component_id", "paths"])
def test_every_component_declares_required_keys(key: str) -> None:
    for entry in components(read()):
        assert entry.get(key), f"{CONFIG.name}:component {entry!r} missing {key}"


def test_no_carryforward_inside_component_management() -> None:
    assert not _carryforward_sites(read()["component_management"])


def test_carryforward_lives_only_in_flag_sections() -> None:
    sites = _carryforward_sites(read())
    assert sites
    assert all(site[0] in _ALLOWED_CARRYFORWARD for site in sites)


def test_flag_management_carries_carryforward() -> None:
    assert read()["flag_management"]["default_rules"]["carryforward"] is True


def test_nightly_does_not_join_the_primary_report() -> None:
    assert read()["flags"]["nightly"]["joined"] is False


def test_components_cover_every_derived_member() -> None:
    derived = set(member_import_names())
    declared = {entry["component_id"] for entry in components(read()) if entry["component_id"] in derived}
    assert declared == derived, f"{CONFIG.name}:individual_components missing {sorted(derived - declared)}"


def test_package_paths_match_derived_import_roots() -> None:
    expected = {Path(root).name: f"{root}/**" for root in import_roots()}
    declared = {
        entry["component_id"]: tuple(entry["paths"])
        for entry in components(read())
        if entry["component_id"] in expected
    }
    assert declared == {name: (glob,) for name, glob in expected.items()}


_OLD_MAPPING: CodecovDoc = {
    "component_management": {
        "default_rules": {"carryforward": True},
        "individual_components": {"battery": {"flag_regexes": ["battery"], "carryforward": True}},
    },
    "flag_management": {"default_rules": {"carryforward": False}},
    "flags": {"nightly": {"joined": True}},
}


def test_old_mapping_form_is_named_by_the_shape_bite() -> None:
    with pytest.raises(AssertionError, match=f"{CONFIG.name}:individual_components"):
        components(_OLD_MAPPING)
