from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


PRIVATE_HOSTS = {"localhost", "host.docker.internal"}
TWO_PART_PUBLIC_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au"}


def normalize_public_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    if not parsed.hostname:
        raise ValueError("invalid_url")

    host = parsed.hostname.lower().rstrip(".")
    if host in PRIVATE_HOSTS:
        raise ValueError("private_network_target")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise ValueError("private_network_target")

    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    path = _strip_source_colon(parsed.path or "")
    query = _strip_source_colon(parsed.query)
    if query:
        query = urlencode(parse_qsl(query, keep_blank_values=True), doseq=True)
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
