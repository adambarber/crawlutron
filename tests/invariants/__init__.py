"""Executable safety invariants, split by what each one is about: AST walks that
verify unsafe patterns are absent from ``app/`` so reintroducing them reddens the
build, themed per review concern -- one file per question.
"""

from __future__ import annotations

__all__: list[str] = []
