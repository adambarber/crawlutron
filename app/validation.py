from __future__ import annotations

import ipaddress
import os
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

PRIVATE_HOSTS = {"localhost", "host.docker.internal"}
TWO_PART_PUBLIC_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au"}

# Attribution/tracking query parameters that carry no content-identity
# information. Dropping them lets URLs that differ only by UTM/tracking tags
# canonicalize to the same key, preventing duplicate fetches of the same page
# (#6). Bare ``ref`` is intentionally excluded: it is ambiguous and frequently
# a legitimate content parameter, so stripping it could collapse distinct pages.
_TRACKING_PARAM_EXACT = frozenset(
    {
        "gclid", "gclsrc", "fbclid", "gbraid", "wbraid",
        "mc_cid", "mc_eid", "mkt_tok", "igshid",
        "_hsenc", "_hsmi", "hsctat",
    }
)


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in _TRACKING_PARAM_EXACT


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
    # Inverted-by-default: any value not explicitly recognized as truthy keeps
    # the SSRF guard ON, mirroring ``settings._parse_bool_env_value`` so the
    # two paths agree on what counts as "on".
    allow_private = parse_bool_env("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", default=False)
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
    path = _strip_trailing_colon(parsed.path or "").rstrip("/")
    query = _strip_trailing_colon(parsed.query)
    if query:
        # Drop known tracking/attribution parameters so URLs that differ only
        # by UTM/tracking tags canonicalize to the same key and are not fetched
        # as duplicate pages (#6). Remaining params are sorted so URLs differing
        # only in parameter order also canonicalize identically.
        pairs = [
            (k, v)
            for (k, v) in parse_qsl(query, keep_blank_values=True)
            if not _is_tracking_param(k)
        ]
        pairs.sort()
        query = urlencode(pairs, doseq=True)
    return urlunparse((parsed.scheme, netloc, path, parsed.params, query, ""))


def _strip_trailing_colon(value: str) -> str:
    return value[:-1] if value.endswith(":") else value


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Read an env var and return a boolean.

    Truthy values: ``1``, ``true``, ``yes``, ``on`` (case-insensitive).
    Anything else (including unset) is treated as the default. Centralized so
    every SSRF / feature-flag check agrees on what "on" means (adversarial
    review F-ssrf-env-var-parsing).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _public_suffix_list():
    """Lazily load the real public-suffix dataset (M5), None when absent.

    The publicsuffixlist package ships a snapshot .dat (no runtime network).
    Cached on first use; any failure falls back to the heuristic below so
    the service never fails to import or gate without the dataset.
    """
    global _PSL
    try:
        return _PSL
    except NameError:
        pass
    try:
        from publicsuffixlist import PublicSuffixList

        _PSL = PublicSuffixList()
    except Exception:
        _PSL = None
    return _PSL


def _heuristic_registrable_domain(host: str) -> str:
    """Fallback used only when the PSL dataset is unavailable."""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in TWO_PART_PUBLIC_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def registrable_domain(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or url).lower().rstrip(".")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except Exception:
        ascii_host = host
    psl = _public_suffix_list()
    if psl is not None:
        try:
            registrable = psl.privatesuffix(ascii_host)
            if registrable:
                return registrable
        except Exception:  # noqa: S110 - deliberate fallthrough to heuristic below
            # A poisoned/unparseable host falls through to the heuristic.
            pass
    return _heuristic_registrable_domain(host)
