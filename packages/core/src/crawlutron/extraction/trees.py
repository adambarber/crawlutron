"""HTML tree parsing, frame/form collection and diagnostics over crawlutron_extract."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from html import unescape
from typing import TypedDict

from crawlutron_extract.html_dom import DOMNode, build_dom
from crawlutron_extract.limits import ExtractLimits
from safe_url.normalized_url import NormalizedUrl

Tree = DOMNode


class FrameDiagnostic(TypedDict):
    """One frame/iframe intercepted, admitted through the source URL's policy."""

    url: str
    normalized_url: str
    name: str
    title: str
    source_url: str | None
    host: str
    is_same_host: bool


class FormDiagnostic(TypedDict):
    """One form and its controls, with the action admitted through policy."""

    method: str
    action: str
    normalized_action: str
    target: str
    name: str
    id: str
    controls: list[dict[str, str]]


class HtmlDiagnostics(TypedDict, total=False):
    """The diagnostics block a parsed page contributes (both sides optional)."""

    frames: list[FrameDiagnostic]
    forms: list[FormDiagnostic]


_parse_tally_lock = threading.Lock()

_parse_tally = 0


def parse_html_tree(html: str | None) -> Tree | None:
    """Build one bounded DOM tree from HTML; None for empty or invalid input.

    Fragments are re-wrapped to an ``html`` root to match the prior lxml shape.
    """
    global _parse_tally
    with _parse_tally_lock:
        _parse_tally += 1
    if not html:
        return None
    root, _truncated = build_dom(html, ExtractLimits().max_dom_nodes, ExtractLimits().max_dom_depth)
    if root is not None and root.tag != "html":
        html_root = DOMNode("html", {}, None)
        html_root.add_child(root)
        root.parent = html_root
        root = html_root
    return root


def _walk(tree: Tree):
    """Yield every DOM node in document order, root first."""
    yield tree
    for child in tree.children:
        yield from _walk(child)


def _frame_elements(tree: Tree):
    for node in _walk(tree):
        if node.tag in ("frame", "iframe"):
            yield node


def _form_elements(tree: Tree):
    for node in _walk(tree):
        if node.tag == "form":
            yield node


@dataclass(frozen=True)
class _Walkable:
    """A source URL and the DOM tree to walk, both known good."""

    source_url: NormalizedUrl
    tree: Tree


def walkable(
    html: str | None, source_url: NormalizedUrl | None, tree: Tree | None = None
) -> _Walkable | None:
    """Preconditions for a diagnostic extractor: a source URL and a usable tree."""
    if not source_url:
        return None
    if tree is None:
        tree = parse_html_tree(html)
    if tree is None:
        return None
    return _Walkable(source_url=source_url, tree=tree)


class FrameCollector:
    """Back-compat collector: build one DOM tree and collect frame/iframe srcs."""

    def __init__(self, source_url: NormalizedUrl) -> None:
        self.source_url = source_url
        self.frames: list[dict[str, str]] = []

    def feed(self, html: str) -> None:
        tree = parse_html_tree(html)
        if tree is None:
            return
        seen: set[str] = set()
        for el in _frame_elements(tree):
            src = (el.get_attribute("src") or "").strip()
            if not src:
                continue
            normalized = self.source_url.resolve(src)
            if normalized is None or normalized.raw in seen:
                continue
            seen.add(normalized.raw)
            self.frames.append(
                {
                    "url": normalized.raw,
                    "name": el.get_attribute("name") or "",
                    "title": "",
                }
            )


def _cap_attr(value: str, limits: ExtractLimits) -> str:
    """Truncate one attribute value to the per-attribute char cap."""
    if len(value) > limits.max_attr_chars:
        return value[: limits.max_attr_chars]
    return value


def frames_from(
    walkable: _Walkable,
    source_url: NormalizedUrl | None = None,
    *,
    limits: ExtractLimits | None = None,
) -> list[FrameDiagnostic]:
    """Frame/iframe diagnostics admitted through the source URL's policy."""
    if limits is None:
        limits = ExtractLimits()
    source_host = walkable.source_url.host
    frames: list[FrameDiagnostic] = []
    seen: set[NormalizedUrl] = set()
    for el in _frame_elements(walkable.tree):
        if len(frames) >= limits.max_frames:
            break
        src = (el.get_attribute("src") or "").strip()
        if not src:
            continue
        normalized_url = walkable.source_url.resolve(src)
        if normalized_url is None or normalized_url in seen:
            continue
        seen.add(normalized_url)
        frame_host = normalized_url.host
        frames.append(
            {
                "url": normalized_url.raw,
                "normalized_url": normalized_url.raw,
                "name": _cap_attr(el.get_attribute("name") or "", limits),
                "title": _cap_attr(el.get_attribute("title") or "", limits),
                "source_url": source_url.raw if source_url is not None else None,
                "host": frame_host,
                "is_same_host": bool(frame_host) and frame_host == source_host,
            }
        )
    return frames


_CONTROL_TAGS = ("input", "select", "textarea", "button")


def forms_from(
    walkable: _Walkable,
    *,
    limits: ExtractLimits | None = None,
) -> list[FormDiagnostic]:
    if limits is None:
        limits = ExtractLimits()
    forms: list[FormDiagnostic] = []
    for form_el in _form_elements(walkable.tree):
        if len(forms) >= limits.max_forms:
            break
        attrs = {key.lower(): value for key, value in form_el.attrs.items()}
        action = unescape(str(attrs.get("action", "")).strip())
        normalized_action = walkable.source_url.resolve(action or walkable.source_url.raw)
        if normalized_action is None:
            continue
        controls: list[dict[str, str]] = []
        for node in _walk(form_el):
            if node.tag not in _CONTROL_TAGS:
                continue
            cattrs = {key.lower(): value for key, value in node.attrs.items()}
            tag = node.tag
            control_type = (
                (cattrs.get("type") or "text").lower() if tag == "input" else cattrs.get("type", "")
            )
            controls.append(
                {
                    "tag": tag,
                    "type": control_type,
                    "name": _cap_attr(cattrs.get("name", ""), limits),
                    "id": _cap_attr(cattrs.get("id", ""), limits),
                    "value": _cap_attr(cattrs.get("value", ""), limits),
                }
            )
        forms.append(
            {
                "method": (attrs.get("method") or "GET").upper(),
                "action": normalized_action.raw,
                "normalized_action": normalized_action.raw,
                "target": _cap_attr(attrs.get("target", ""), limits),
                "name": _cap_attr(attrs.get("name", ""), limits),
                "id": _cap_attr(attrs.get("id", ""), limits),
                "controls": controls,
            }
        )
    return forms


def html_diagnostics(
    html: str | None,
    source_url: NormalizedUrl | None,
    *,
    tree: Tree | None = None,
    limits: ExtractLimits | None = None,
) -> HtmlDiagnostics:
    """Frame and form diagnostics for one page: one parse, one guard."""
    if limits is None:
        limits = ExtractLimits()
    diagnostics: HtmlDiagnostics = {}
    walked = walkable(html, source_url, tree)
    if walked is None:
        return diagnostics
    frames = frames_from(walked, source_url, limits=limits)
    forms = forms_from(walked, limits=limits)
    if frames:
        diagnostics["frames"] = frames
    if forms:
        diagnostics["forms"] = forms
    return diagnostics
