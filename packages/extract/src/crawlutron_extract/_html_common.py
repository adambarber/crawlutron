"""Shared HTML parse helpers over html.parser attr lists."""

from __future__ import annotations

from html import parser as html_parser

from crawlutron_extract.limits import ExtractLimits
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


def _prepare(
    html: bytes | str,
    source_url: str | NormalizedUrl | None,
    limits: ExtractLimits | None = None,
) -> tuple[str, NormalizedUrl | None, ExtractLimits]:
    """Decode html, admit the source URL, and apply the default limits."""
    if limits is None:
        limits = ExtractLimits()
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    base: NormalizedUrl | None = None
    if isinstance(source_url, NormalizedUrl):
        base = source_url
    elif source_url is not None:
        admitted = admit_or_none(source_url, policy=HostPolicy.unrestricted())
        if admitted is not None:
            base = admitted
    return text, base, limits


def tag_attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    """html.parser's (name, value) pairs as a string dict, values normalised."""
    return {name: (value or "") for name, value in attrs}
