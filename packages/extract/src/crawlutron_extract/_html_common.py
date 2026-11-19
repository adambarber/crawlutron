"""Shared HTML parse helpers over html.parser attr lists."""

from __future__ import annotations

__all__ = ["tag_attrs"]


def tag_attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    """html.parser's (name, value) pairs as a string dict, values normalised."""
    return {name: (value or "") for name, value in attrs}
