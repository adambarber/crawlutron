"""Link collection: anchor resolution, <base href>, safe_url admission.
Collected document-wide for frontier discovery.
"""

from __future__ import annotations

from safe_url import HostPolicy, NormalizedUrl, admit_or_none

from crawlutron_extract.limits import ExtractLimits


class LinkCollector:
    """Collects and admits <a> links during one parse pass."""

    def __init__(self, limits: ExtractLimits, policy: HostPolicy | None = None) -> None:
        self._limits = limits
        self._links: list[NormalizedUrl] = []
        self._seen_links: set[str] = set()
        self._links_refused = 0
        self._links_truncated = False
        self._base_url: NormalizedUrl | None = None
        self._policy = policy
        self._in_link = False
        self._link_url = ""
        self._link_text_parts: list[str] = []

    def set_base_url(self, base_url: NormalizedUrl | None) -> None:
        self._base_url = base_url

    def is_in_link(self) -> bool:
        return self._in_link

    def handle_starttag(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "a":
            self._in_link = True
            self._link_url = attrs.get("href", "")
            self._link_text_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._link_text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._close_link()

    def _close_link(self) -> None:
        if not self._in_link:
            return
        self._in_link = False
        text = " ".join("".join(self._link_text_parts).split())
        if not text and not self._link_url:
            self._link_url = ""
            self._link_text_parts = []
            return
        url = self._link_url
        if self._base_url is not None:
            resolved = self._base_url.resolve(url)
            if resolved is not None:
                url = resolved._raw
        admitted = admit_or_none(url, policy=self._policy) if self._policy is not None else None
        if admitted is None:
            self._links_refused += 1
        elif admitted.raw not in self._seen_links:
            self._seen_links.add(admitted.raw)
            if len(self._links) >= self._limits.max_links:
                self._links_truncated = True
            else:
                self._links.append(admitted)
        self._link_url = ""
        self._link_text_parts = []

    def result(self) -> tuple[list[NormalizedUrl], int]:
        return self._links, self._links_refused

    def add_link(self, link: NormalizedUrl) -> None:
        if link.raw not in self._seen_links:
            self._seen_links.add(link.raw)
            if len(self._links) >= self._limits.max_links:
                return
            self._links.append(link)

    def add_links_refused(self, count: int) -> None:
        self._links_refused += count
