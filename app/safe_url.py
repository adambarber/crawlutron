"""Validated-URL chokepoint: make the safe state the only representable state.

SSRF defense was five functions each call site had to compose correctly
(``normalize_public_http_url``, ``check_ssrf_protection``,
``resolve_and_pin_host``, ``_assert_safe_final_url``,
``validate_webhook_url``); every redirect finding was "a code path held a
raw ``str`` URL and forgot one of the five steps."

``SafeUrl`` folds normalize + check (+ pin) into :meth:`parse`, and
redirects are only expressible via :meth:`redirect_hop` -- there is no API
surface for an unvalidated hop. New code that fetches an
attacker-influenced URL must hold a ``SafeUrl``, not a ``str``.

Heavy SSRF machinery (``app.crawl.strategies.http``) is imported
function-locally: that module pulls the cffi/browser stack, and the two
existing callers (document extractor, webhook delivery) already isolate it
the same way. ``app.validation`` (normalize) is light and imported up top.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from app.models.normalized_url import normalize_public_http_url
from app.validation import HostPolicy


@dataclass(frozen=True)
class SafeUrl:
    """A URL that has passed normalization + SSRF validation.

    ``raw`` is the normalized URL (post ``normalize_public_http_url``).
    ``pinned_ip`` is set only when parsed with ``pin=True``: the exact IP
    the connection must use (DNS-rebinding guard).
    """

    raw: str
    hostname: str
    policy: HostPolicy
    pinned_ip: str | None = None

    @classmethod
    def parse(cls, url: str, *, policy: HostPolicy, pin: bool = False) -> SafeUrl:
        """Normalize + SSRF-check ``url`` in one place; raise ValueError if unsafe.

        With ``pin=True`` also resolve-and-pin the host (HTTPS delivery and
        other paths that must connect to a validated IP, not re-resolve).
        """
        from app.crawl.strategies.pinned_transport import check_ssrf_protection, resolve_and_pin_host

        normalized = normalize_public_http_url(url, policy=policy)
        check_ssrf_protection(normalized)
        hostname = urlparse(normalized).hostname or ""
        pinned: str | None = None
        if pin:
            _, pinned = resolve_and_pin_host(normalized)
        return cls(raw=normalized, hostname=hostname, policy=policy, pinned_ip=pinned)

    def redirect_hop(self, location: str) -> SafeUrl:
        """Follow one redirect: resolve against this URL, re-validate, return new SafeUrl.

        The ONLY way to express a redirect hop. Preserves the pin preference:
        a pinned URL re-pins the new host; an unpinned one stays unpinned.
        Raises ValueError when the hop target is unsafe.
        """
        return SafeUrl.parse(
            urljoin(self.raw, location),
            policy=self.policy,
            pin=self.pinned_ip is not None,
        )

    def connect(self) -> tuple[str, dict[str, str]]:
        """Return ``(connect_url, headers)`` applying the pin policy.

        Plain HTTP connects to the exact validated ``pinned_ip`` (closing the
        DNS-rebinding TOCTOU between parse-time validation and connect-time
        re-resolution) with a ``Host`` header preserving the original hostname
        for virtual hosting. HTTPS keeps the hostname so TLS SNI / certificate
        verification works (real certs carry no IP SAN); there the rebinding
        window is bounded by CA validation plus the pre-connect IP check,
        matching the sibling HTTP fetcher.

        Raises ValueError when an HTTP URL was parsed without pinning: the
        unsafe fallback (connect-by-name, which re-resolves DNS) is made
        unrepresentable rather than silently reachable.
        """
        parsed = urlparse(self.raw)
        if parsed.scheme == "https":
            return self.raw, {}
        if not self.pinned_ip:
            raise ValueError(f"unpinned http URL cannot connect safely: {self.raw!r}")
        port = f":{parsed.port}" if parsed.port else ""
        # An IPv6 literal must be bracketed in a URL netloc or its colons
        # corrupt port/path parsing; getaddrinfo can return v6.
        host = f"[{self.pinned_ip}]" if ":" in self.pinned_ip else self.pinned_ip
        connect_url = f"{parsed.scheme}://{host}{port}{parsed.path or '/'}"
        if parsed.query:
            connect_url = f"{connect_url}?{parsed.query}"
        headers: dict[str, str] = {}
        if parsed.hostname:
            headers["Host"] = f"{parsed.hostname}{port}"
        return connect_url, headers


__all__ = ["SafeUrl"]
