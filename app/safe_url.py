"""Validated-URL chokepoint: make the safe state the only representable state.

SSRF defense was five functions each call site had to compose correctly
(``normalize_public_http_url``, ``_check_ssrf_protection``,
``_resolve_and_pin_host``, ``_assert_safe_final_url``,
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

from app.validation import normalize_public_http_url


@dataclass(frozen=True)
class SafeUrl:
    """A URL that has passed normalization + SSRF validation.

    ``raw`` is the normalized URL (post ``normalize_public_http_url``).
    ``pinned_ip`` is set only when parsed with ``pin=True``: the exact IP
    the connection must use (DNS-rebinding guard).
    """

    raw: str
    hostname: str
    pinned_ip: str | None = None

    @classmethod
    def parse(cls, url: str, *, pin: bool = False) -> SafeUrl:
        """Normalize + SSRF-check ``url`` in one place; raise ValueError if unsafe.

        With ``pin=True`` also resolve-and-pin the host (HTTPS delivery and
        other paths that must connect to a validated IP, not re-resolve).
        """
        from app.crawl.strategies.http import _check_ssrf_protection, _resolve_and_pin_host

        normalized = normalize_public_http_url(url)
        _check_ssrf_protection(normalized)
        hostname = urlparse(normalized).hostname or ""
        pinned: str | None = None
        if pin:
            _, pinned = _resolve_and_pin_host(normalized)
        return cls(raw=normalized, hostname=hostname, pinned_ip=pinned)

    def redirect_hop(self, location: str) -> SafeUrl:
        """Follow one redirect: resolve against this URL, re-validate, return new SafeUrl.

        The ONLY way to express a redirect hop. Preserves the pin preference:
        a pinned URL re-pins the new host; an unpinned one stays unpinned.
        Raises ValueError when the hop target is unsafe.
        """
        return SafeUrl.parse(urljoin(self.raw, location), pin=self.pinned_ip is not None)
