"""Shared HTML tree parsing, frame/form collection and diagnostics."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from html import unescape
from typing import Any, TypedDict

from lxml import etree
from lxml.etree import ParserError

from app.models.normalized_url import NormalizedUrl

# --- Structured discovery diagnostics: links, frames, forms ---


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


# Tally over the real parser, not a patched one; locked because worker threads parse.
_parse_tally_lock = threading.Lock()


_parse_tally = 0


def parse_count() -> int:
    """How many lxml trees this process has parsed.

    Cumulative and monotonic: a caller measuring one stage brackets it and takes
    the difference, so a budget that spans several pipeline phases is expressible
    without production code having to know where the phase boundaries are.
    """
    with _parse_tally_lock:
        return _parse_tally


def parse_html_tree(html: str | None):
    """Parse HTML once with lxml and return the root element.

    All discovery passes (anchors, frames, forms) and the XPath selector path
    reuse this single parse so we allocate and walk the tree only once instead
    of running three separate :class:`HTMLParser` subclasses.

    ``lxml.etree.HTML`` (rather than ``lxml.html.fromstring``) is used because
    it wraps fragments in a document tree and preserves legacy ``<frameset>`` /
    ``<frame>`` markup that the lenient ``fromstring`` path otherwise drops.
    Returns ``None`` for empty/invalid input.
    """
    global _parse_tally
    with _parse_tally_lock:
        _parse_tally += 1
    if not html:
        return None
    try:
        return etree.HTML(html)
    except (ParserError, ValueError, TypeError):
        return None


def _diagnostic_tree(html: str | None, tree: Any | None) -> Any | None:
    return tree if tree is not None else (parse_html_tree(html) if html else None)


def _diagnostic_inputs(html: str | None, tree: Any | None) -> Any | None:
    return _diagnostic_tree(html, tree) if html else None


@dataclass(frozen=True)
class _Walkable:
    """A source URL and the tree to walk, both known good."""

    source_url: NormalizedUrl
    tree: Any


def walkable(html: str | None, source_url: NormalizedUrl | None, tree: Any | None) -> _Walkable | None:
    """What a diagnostic extractor needs before it can look at anything, or ``None``.

    Three facts, and each one is a precondition the extractors all share: there is
    a source URL to attribute a finding to, the HTML is parsed, and the parse
    produced a tree. Stated per extractor it reads as three separate guards, and
    it is the ``tree is None`` one that gets dropped -- a diagnostic that walks
    ``None`` reports nothing, which is a passing test and a permanently dead
    signal.

    A record rather than the bare tree so the caller gets the *narrowed* URL back.
    Handing back the tree alone leaves every caller's ``source_url`` typed
    ``str | None`` even though the None case has just returned, which is how a
    chokepoint ends up pushing its own precondition back onto the call sites.
    """
    if not source_url:
        return None
    parsed = _diagnostic_inputs(html, tree)
    return None if parsed is None else _Walkable(source_url=source_url, tree=parsed)


class FrameCollector:
    """Back-compat collector: parse once with lxml and collect frame/iframe srcs."""

    def __init__(self, source_url: NormalizedUrl) -> None:
        self.source_url = source_url
        self.frames: list[dict[str, str]] = []

    def feed(self, html: str) -> None:
        tree = _diagnostic_tree(html, None)
        if tree is None:
            return
        for el in tree.iter("frame", "iframe"):
            src = (el.get("src") or "").strip()
            normalized = self.source_url.resolve(src)
            if normalized is None:
                continue
            self.frames.append(
                {
                    "url": normalized.raw,
                    "name": el.get("name") or "",
                    "title": el.get("title") or "",
                }
            )


def frames_from(
    walkable: _Walkable,
    source_url: NormalizedUrl | None,
) -> list[FrameDiagnostic]:
    """Frame/iframe diagnostics, admitted through the source URL's own policy.

    ``resolve`` on the source value is the filter: a frame the job's policy
    refuses is dropped here, and there is no second policy parameter to pass
    a different answer to.
    """
    source_host = walkable.source_url.host
    frames: list[FrameDiagnostic] = []
    seen: set[NormalizedUrl] = set()
    for el in walkable.tree.iter("frame", "iframe"):
        src = (el.get("src") or "").strip()
        if not src:
            continue
        normalized_url = walkable.source_url.resolve(src)
        if normalized_url is None:
            continue
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        frame_host = normalized_url.host
        frames.append(
            {
                "url": normalized_url.raw,
                "normalized_url": normalized_url.raw,
                "name": el.get("name") or "",
                "title": el.get("title") or "",
                "source_url": source_url.raw if source_url is not None else None,
                "host": frame_host,
                "is_same_host": bool(frame_host) and frame_host == source_host,
            }
        )
    return frames


_CONTROL_TAGS = ("input", "select", "textarea", "button")


def forms_from(
    walkable: _Walkable,
) -> list[FormDiagnostic]:
    forms: list[FormDiagnostic] = []
    for form_el in walkable.tree.iter("form"):
        attrs = {key.lower(): value for key, value in form_el.attrib.items()}
        action = unescape(str(attrs.get("action", "")).strip())
        normalized_action = walkable.source_url.resolve(action or walkable.source_url.raw)
        if normalized_action is None:
            continue
        controls: list[dict[str, str]] = []
        for control in form_el.iter(*_CONTROL_TAGS):
            cattrs = {key.lower(): value for key, value in control.attrib.items()}
            tag = str(control.tag)
            control_type = (
                (cattrs.get("type") or "text").lower() if tag == "input" else cattrs.get("type", "")
            )
            controls.append(
                {
                    "tag": tag,
                    "type": control_type,
                    "name": cattrs.get("name", ""),
                    "id": cattrs.get("id", ""),
                    "value": cattrs.get("value", ""),
                }
            )
        forms.append(
            {
                "method": (attrs.get("method") or "GET").upper(),
                "action": normalized_action.raw,
                "normalized_action": normalized_action.raw,
                "target": attrs.get("target", ""),
                "name": attrs.get("name", ""),
                "id": attrs.get("id", ""),
                "controls": controls,
            }
        )
    return forms


def html_diagnostics(
    html: str | None,
    source_url: NormalizedUrl | None,
    *,
    tree: Any | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    # One parse, one guard: the walkable-tree precondition (empty diagnostic,
    # not an error) is stated once, not spelled in front of both extractors.
    walked = walkable(html, source_url, tree)
    if walked is None:
        return diagnostics
    frames = frames_from(walked, source_url)
    forms = forms_from(walked)
    if frames:
        diagnostics["frames"] = frames
    if forms:
        diagnostics["forms"] = forms
    return diagnostics
