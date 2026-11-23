"""Strict typed projections of .pre-commit-config.yaml and the prepare action."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from tests._ci_contract import CONFIG_PATH, PREPARE_ACTION_PATH

STRICT = ConfigDict(extra="forbid", frozen=True)


class Hook(BaseModel):
    model_config = STRICT
    id: str = Field(min_length=1)
    stages: tuple[str, ...]
    entry: str
    files: str | None = None
    line: int


class Precommit(BaseModel):
    model_config = STRICT
    path: Path
    hooks: tuple[Hook, ...]

    def by_id(self, hook_id: str) -> Hook | None:
        return next((hook for hook in self.hooks if hook.id == hook_id), None)


class ActionStep(BaseModel):
    model_config = STRICT
    uses: str | None = None
    run: str | None = None
    inputs: Mapping[str, str] = Field(default_factory=dict)
    line: int


class CompositeAction(BaseModel):
    model_config = STRICT
    path: Path
    steps: tuple[ActionStep, ...]


def parse_precommit(path: Path) -> Precommit:
    """Parse the hook table once; rules never see the YAML text."""
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    repos = document.get("repos") if isinstance(document, dict) else None
    if not isinstance(repos, list):
        raise ValueError(f"{path.name} has no repos list")
    hook_lines = _id_lines(text)
    hooks: list[Hook] = []
    for repo in repos:
        declared = repo.get("hooks") if isinstance(repo, dict) else None
        if not isinstance(declared, list):
            continue
        for raw in declared:
            if not isinstance(raw, dict) or "id" not in raw:
                continue
            hook_id = str(raw["id"])
            stages = raw.get("stages") or ["pre-commit"]
            hooks.append(
                Hook(
                    id=hook_id,
                    stages=tuple(str(stage) for stage in stages),
                    entry=str(raw.get("entry", "")),
                    files=None if raw.get("files") is None else str(raw["files"]),
                    line=hook_lines.get(hook_id, 0),
                )
            )
    if not hooks:
        raise ValueError(f"{path.name} declares no hooks")
    return Precommit(path=path, hooks=tuple(hooks))


def _id_lines(text: str) -> dict[str, int]:
    """Locate hook ids once, at parse time, so findings carry a real line."""
    found: dict[str, int] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("- id:"):
            found[stripped.split(":", 1)[1].strip()] = number
    return found


def parse_action(path: Path) -> CompositeAction:
    """Parse a composite action's steps into strict objects with source lines."""
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    runs = document.get("runs") if isinstance(document, dict) else None
    steps = runs.get("steps") if isinstance(runs, dict) else None
    if not isinstance(steps, list):
        raise ValueError(f"{path.name} declares no composite steps")
    lines = _step_lines(text)
    parsed: list[ActionStep] = []
    for index, raw in enumerate(steps):
        if not isinstance(raw, dict):
            continue
        with_block = raw.get("with")
        with_block = with_block if isinstance(with_block, dict) else {}
        parsed.append(
            ActionStep(
                uses=None if raw.get("uses") is None else str(raw["uses"]),
                run=None if raw.get("run") is None else str(raw["run"]),
                inputs={str(key): str(value) for key, value in with_block.items()},
                line=lines[index] if index < len(lines) else 0,
            )
        )
    return CompositeAction(path=path, steps=tuple(parsed))


def _step_lines(text: str) -> list[int]:
    """Line of each `- uses:`/`- name:` step opener, in order."""
    return [number for number, line in enumerate(text.splitlines(), start=1) if line.strip().startswith("- ")]


PRECOMMIT = parse_precommit(CONFIG_PATH)
PREPARE = parse_action(PREPARE_ACTION_PATH)
