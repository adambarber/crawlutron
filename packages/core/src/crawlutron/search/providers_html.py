"""The keyless DuckDuckGo provider: a scraped HTML page parsed with lxml.

This provider is a different shape from the keyed family -- no API key, a page
scraped and parsed -- so it is a separate module rather than another subclass.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlsplit

from lxml import html as lxml_html
from safe_url.host_policy import HostPolicy

from crawlutron.search import providers as _providers
from crawlutron.search.providers import (
    DUCKDUCKGO_REDIRECT,
    DUCKDUCKGO_URL,
    BaseProvider,
    SearchProviderError,
    SearchResult,
    clip_text,
    normalize_result_url,
)

_DUCKDUCKGO_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_DDG_UNRELIABLE = (
    "the keyless provider is unreliable from datacenter IPs — configure a keyed"
    " provider via CRAWLUTRON_*_API_KEY"
)


class DuckDuckGoProvider(BaseProvider):
    """Keyless DuckDuckGo HTML endpoint (best-effort; captcha-prone from datacenters)."""

    name = "duckduckgo"

    async def _fetch(self, query: str, limit: int, timeout_seconds: float) -> tuple[int, str]:
        """Post the query to the scraped HTML endpoint."""
        return await _providers.provider_http(
            "POST",
            DUCKDUCKGO_URL,
            data={"q": query},
            headers={"User-Agent": _DUCKDUCKGO_UA},
            timeout_seconds=timeout_seconds,
        )

    def _results(self, status: int, body: str, limit: int) -> list[SearchResult]:
        """Parse the scraped page into normalized results."""
        if status != 200:
            raise SearchProviderError(f"duckduckgo HTTP {status}; {_DDG_UNRELIABLE}")
        lowered = body.lower()
        if "anomaly" in lowered or "challenge to confirm" in lowered:
            raise SearchProviderError(f"duckduckgo returned a bot challenge; {_DDG_UNRELIABLE}")
        results: list[SearchResult] = []
        seen: set[str] = set()
        # the thing that SHOULD fail here: this import used to sit inside the
        # `except Exception` below, so a missing lxml read as a fault.
        try:
            tree = lxml_html.fromstring(body)
        except Exception:
            raise SearchProviderError("duckduckgo returned unparsable HTML") from None
        for node in tree.xpath("//div[contains(concat(' ', normalize-space(@class), ' '), ' result ')]"):
            link = node.xpath(".//a[contains(@class, 'result__a')]")
            if not link:
                continue
            url = _duckduckgo_target(
                str(link[0].get("href") or ""),
                policy=self._host_policy,
            )
            if not url:
                continue
            normalized = normalize_result_url(url, policy=self._host_policy)
            if not normalized or normalized in seen:
                continue
            snippet_node = node.xpath(".//a[contains(@class, 'result__snippet')]") or node.xpath(
                ".//*[contains(@class, 'result__snippet')]"
            )
            seen.add(normalized)
            results.append(
                SearchResult(
                    url=normalized,
                    title=clip_text(" ".join(link[0].text_content().split())),
                    description=clip_text(" ".join(snippet_node[0].text_content().split()))
                    if snippet_node
                    else "",
                )
            )
            if len(results) >= limit:
                break
        return results


def _duckduckgo_target(href: str, *, policy: HostPolicy) -> str | None:
    """Resolve a DDG result link: unwrap the ``//duckduckgo.com/l/?uddg=<enc>``
    wrapper to the encoded target when present, else admit ``href`` itself."""
    if href.startswith(DUCKDUCKGO_REDIRECT):
        parsed = urlsplit(href.removeprefix(DUCKDUCKGO_REDIRECT))
        uddg = parse_qs(parsed.query).get("uddg")
        if uddg:
            return normalize_result_url(unquote(uddg[0]), policy=policy)
        return None
    return normalize_result_url(href, policy=policy)


__all__ = ["DuckDuckGoProvider"]
