"""Shared HTML parse helpers over html.parser attr lists."""

from __future__ import annotations

from html import parser as html_parser

from safe_url import HostPolicy, NormalizedUrl, admit_or_none

__all__ = ["resolve_or_admit", "tag_attrs"]


class _HtmlParser(html_parser.HTMLParser):
    """Base parser for single-pass HTML discovery: charrefs converted."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)


def resolve_or_admit(href: str, target: NormalizedUrl | None) -> NormalizedUrl | None:
    """Resolve href against target, or admit it outright when there is no base URL."""
    if target is not None:
        return target.resolve(href)
    return admit_or_none(href, policy=HostPolicy.unrestricted())


def tag_attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    """html.parser's (name, value) pairs as a string dict, values normalised."""
    return {name: (value or "") for name, value in attrs}
