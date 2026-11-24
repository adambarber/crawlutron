"""Every actions/cache key on disk, admitted once and rendered without literal braces."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from tests._ci_contract import workflow_files
from tests.workflows._admission import admit_or_none
from tests.workflows._model import WORKFLOW_DIR, parse_document
from tests.workflows._names import ExpressionText
from tests.workflows._policy import Finding

_CACHE_USES = "actions/cache@"
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
_RENDERED = "rendered"
_FIELDS = ("key", "restore-keys")
_STRICT = ConfigDict(extra="forbid", frozen=True)


class CacheKey(BaseModel):
    """One cache key admitted through the expression type."""

    model_config = _STRICT
    key: ExpressionText
    line: int
    source: str

    @classmethod
    def create(cls, raw: object) -> CacheKey:
        return cls.model_validate(raw)

    @classmethod
    def admit_or_none(cls, raw: object) -> CacheKey | None:
        return admit_or_none(cls.create, raw)


def cache_documents() -> tuple[Path, ...]:
    """Workflows plus every composite action that can hold a cache step."""
    actions = WORKFLOW_DIR.parent / "actions"
    return workflow_files(WORKFLOW_DIR) + tuple(sorted(actions.glob("*/action.yml")))


def _cache_steps(
    node: object, path: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], Mapping[str, object]]]:
    if isinstance(node, Mapping):
        uses = node.get("uses")
        if isinstance(uses, str) and uses.startswith(_CACHE_USES):
            yield path, node
        for key, value in node.items():
            yield from _cache_steps(value, (*path, str(key)))
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, item in enumerate(node):
            yield from _cache_steps(item, (*path, str(index)))


def _key_values(raw: object) -> Iterator[str]:
    if isinstance(raw, str):
        for line in raw.splitlines():
            if line.strip():
                yield line.strip()
    elif isinstance(raw, Sequence):
        for item in raw:
            if isinstance(item, str) and item.strip():
                yield item.strip()


def find_unrendered_cache_keys() -> Iterator[Finding]:
    """Refuse any cache key whose expressions cannot be fully rendered."""
    for path in cache_documents():
        document, index = parse_document(path)
        for step_path, step in _cache_steps(document, ()):
            with_block = step.get("with")
            if not isinstance(with_block, Mapping):
                continue
            for field in _FIELDS:
                if field not in with_block:
                    continue
                line = index.get((*step_path, "with", field), 0)
                for value in _key_values(with_block[field]):
                    admitted = CacheKey.admit_or_none({"key": value, "line": line, "source": path.name})
                    if admitted is None:
                        yield Finding(
                            "cache-key-admission",
                            path.stem,
                            "",
                            line,
                            f"{path.name}:{line}: malformed cache expression in {field}: {value!r}",
                        )
                        continue
                    rendered = _EXPRESSION.sub(_RENDERED, admitted.key)
                    if "{{" in rendered or "}}" in rendered:
                        yield Finding(
                            "cache-key-render",
                            path.stem,
                            "",
                            line,
                            f"{path.name}:{line}: unrendered expression in cache {field}: {rendered!r}",
                        )
