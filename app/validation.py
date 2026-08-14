from __future__ import annotations

import ipaddress
import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


PRIVATE_HOSTS = {"localhost", "host.docker.internal"}
TWO_PART_PUBLIC_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au"}


def normalize_public_http_url(url: str) -> str:
    """Canonicalize a public HTTP(S) URL into a single normalized form.

    This is the single source of truth for what a normalized URL is across the
    service: it is used for the crawl target URL and for every discovered link.
    Canonicalization lowercases the scheme and host, drops default ports
    (``:80`` for http, ``:443`` for https), removes the fragment, strips a
    trailing slash from the path (so ``https://example.gov/`` and
    ``https://example.gov`` are identical), and re-encodes the query string.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    if not parsed.hostname:
        raise ValueError("invalid_url")

    host = parsed.hostname.lower().rstrip(".")
    # Local dev/test escape hatch: the crawler is a localhost dev service and
    # tests run a real in-process server on loopback. Allow opting out of the
    # private-network guard via env var rather than relaxing it by default.
    allow_private = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "") not in {"", "0", "false", "no"}
    if not allow_private:
        if host in PRIVATE_HOSTS:
            raise ValueError("private_network_target")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                raise ValueError("private_network_target")

    default_port = 80 if parsed.scheme == "http" else 443
    port = parsed.port
    if port == default_port:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = _strip_source_colon(parsed.path or "").rstrip("/")
    query = _strip_source_colon(parsed.query)
    if query:
        # Sort query parameters so that URLs differing only in parameter order
        # canonicalize to the same string (Katana-style deep normalization).
        pairs = parse_qsl(query, keep_blank_values=True)
        pairs.sort()
        query = urlencode(pairs, doseq=True)
    return urlunparse((parsed.scheme, netloc, path, parsed.params, query, ""))


def _strip_source_colon(value: str) -> str:
    return value[:-1] if value.endswith(":") else value


def registrable_domain(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or url).lower().rstrip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in TWO_PART_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])
