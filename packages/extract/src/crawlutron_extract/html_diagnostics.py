"""HTML diagnostics: bounded frame/form/link counts over one parse."""

from __future__ import annotations

import re
from html import parser as html_parser

from safe_url import HostPolicy

from crawlutron_extract.html_forms_frames import FormFrameCollector
from crawlutron_extract.html_links import LinkCollector
from crawlutron_extract.limits import ExtractLimits

__all__ = ["html_diagnostics"]

#: html.parser delivers iframe/frame content as raw text, so nested frames and
#: iframes arrive as markup in handle_data; scan data to count nesting.
_FRAME_TAG_RE = re.compile(r"<(?:iframe|frame)\b", re.IGNORECASE)
_FRAME_SRC_RE = re.compile(r"<(?:iframe|frame)\b[^>]*?src=[\"']([^\"']*)[\"']", re.IGNORECASE)


def html_diagnostics(html: bytes | str) -> dict:
    """Frame/form/link counts and records for ``html``.

    One parse admits links through ``safe_url`` and builds the frames and
    forms records the collectors already own.
    """
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    limits = ExtractLimits()
    links = LinkCollector(limits, policy=HostPolicy.unrestricted())
    collector = FormFrameCollector(limits)
    has_frameset = False

    class _P(html_parser.HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)

        def handle_starttag(self, tag, attrs):
            nonlocal has_frameset
            attr_dict = {k: (v or "") for k, v in attrs}
            if tag == "frameset":
                has_frameset = True
            elif tag == "a":
                links.handle_starttag(tag, attr_dict)
            elif tag in ("iframe", "frame"):
                collector._handle_frame_start(attr_dict)
            else:
                collector.handle_starttag(tag, attr_dict)

        def handle_endtag(self, tag):
            if tag == "a":
                links.handle_endtag(tag)
            collector.handle_endtag(tag)

        def handle_data(self, data):
            links.handle_data(data)
            collector.handle_data(data)
            for match in _FRAME_SRC_RE.finditer(data):
                collector._handle_frame_start(
                    {"src": match.group(1), "srcdoc": "", "name": "", "sandbox": ""}
                )

    parser = _P()
    parser.feed(text)
    parser.close()
    link_list, _links_refused = links.result()
    forms, frames, _flags = collector.result()
    return {
        "frame_count": len(frames),
        "form_count": len(forms),
        "link_count": len(link_list),
        "has_frameset": has_frameset,
        "frames": frames,
        "forms": forms,
    }
