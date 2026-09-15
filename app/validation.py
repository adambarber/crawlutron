from __future__ import annotations

import ipaddress
import logging
import math
import os
from collections.abc import Mapping
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

PRIVATE_HOSTS = {"localhost", "host.docker.internal"}
TWO_PART_PUBLIC_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au"}

# Attribution/tracking query parameters that carry no content-identity
# information. Dropping them lets URLs that differ only by UTM/tracking tags
# canonicalize to the same key, preventing duplicate fetches of the same page
# (#6). Bare ``ref`` is intentionally excluded: it is ambiguous and frequently
# a legitimate content parameter, so stripping it could collapse distinct pages.
_TRACKING_PARAM_EXACT = frozenset(
    {
        "gclid",
        "gclsrc",
        "fbclid",
        "gbraid",
        "wbraid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "igshid",
        "_hsenc",
        "_hsmi",
        "hsctat",
    }
)


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in _TRACKING_PARAM_EXACT


def _resolver_numeric_part(part: str) -> int | None:
    """Parse one numeric IP component the way glibc-style resolvers do."""
    if not part:
        return None
    lowered = part.lower()
    if lowered.startswith("0x"):
        digits = lowered[2:]
        if not digits or any(c not in "0123456789abcdef" for c in digits):
            return None
        return int(digits, 16)
    if len(part) > 1 and part.startswith("0"):
        if any(c not in "01234567" for c in part):
            return None
        return int(part, 8)
    if not part.isdigit():
        return None
    return int(part, 10)


def _resolver_numeric_ipv4(host: str) -> int | None:
    """Interpret *host* as IPv4 the way resolvers parse numeric forms (L4).

    ``ipaddress`` rejects bare integers (``2130706433``), hex
    (``0x7f000001``), and short dotted quads (``127.1``), but resolvers
    accept them -- so layer 1 must classify the packed value itself instead
    of treating every unparseable host as a plain hostname. Returns the
    32-bit value, or None when the host is not a numeric form (or is out of
    range, which resolvers also reject).
    """
    if not host or any(c not in "0123456789abcdefxABCDEF." for c in host):
        return None
    if "." not in host:
        value = _resolver_numeric_part(host)
        if value is None:
            return None
        # Oversized single integers wrap mod 2**32 on resolvers (verified
        # against getaddrinfo); classify the effective address, not None.
        return value & 0xFFFFFFFF
    parts = host.split(".")
    if not 2 <= len(parts) <= 4:
        return None
    values = []
    for part in parts:
        parsed = _resolver_numeric_part(part)
        if parsed is None:
            return None
        values.append(parsed)
    width = len(values)
    for leading in values[:-1]:
        if leading > 0xFF:
            return None
    last_limit = 1 << (8 * (5 - width))
    if values[-1] >= last_limit:
        return None
    result = 0
    for index, value in enumerate(values[:-1]):
        result |= value << (8 * (3 - index))
    return result | values[-1]


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
    # two paths agree on what counts as "on". Both layers below are memoized
    # (perf tier-2 T2) because this function runs per anchor: the env decision
    # is keyed on its raw value (so any change is visible on the very next
    # call) and the host verdict is a pure function of the canonical host.
    if not _allow_private_targets() and not _host_is_public(host):
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
        pairs = [(k, v) for (k, v) in parse_qsl(query, keep_blank_values=True) if not _is_tracking_param(k)]
        pairs.sort()
        query = urlencode(pairs, doseq=True)
    return urlunparse((parsed.scheme, netloc, path, parsed.params, query, ""))


def _strip_trailing_colon(value: str) -> str:
    return value[:-1] if value.endswith(":") else value


# (raw env value, parsed decision) for BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS.
# Value-keyed, not time-keyed: an env change (including a test monkeypatch)
# takes effect on the very next call; an unchanged value skips the parse.
_PRIVATE_TARGET_DECISION: tuple[str | None, bool] | None = None


def _allow_private_targets() -> bool:
    """Memoized read of the private-target escape hatch (inverted default).

    The raw env value is the cache key, so unrecognized values keep resolving
    to ``False`` (guard ON) through the same ``parse_bool_env`` chokepoint and
    every distinct value change is honored immediately.
    """
    global _PRIVATE_TARGET_DECISION
    raw = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS")
    cached = _PRIVATE_TARGET_DECISION
    if cached is not None and cached[0] == raw:
        return cached[1]
    decision = parse_bool_env("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", default=False)
    _PRIVATE_TARGET_DECISION = (raw, decision)
    return decision


def _reset_guard_memo() -> None:
    """Drop the memoized env decision (tests that count parse calls only).

    Host verdicts are a pure function of the host and never go stale, so the
    verdict memo deliberately is not touched here.
    """
    global _PRIVATE_TARGET_DECISION
    _PRIVATE_TARGET_DECISION = None


@lru_cache(maxsize=8192)
def _host_is_public(host: str) -> bool:
    """SSRF verdict for one canonical host (bounded memo, pure function).

    Deterministic for a given host and independent of the env hatch (only
    consulted while the guard is ON), so cache entries never go stale. The
    bound keeps attacker-influenced host floods from growing memory.
    """
    if host in PRIVATE_HOSTS:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None:
        # Resolver-numeric forms ipaddress rejects (L4): classify the
        # packed value with the same predicate set.
        numeric = _resolver_numeric_ipv4(host)
        if numeric is not None:
            address = ipaddress.ip_address(numeric)
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
    ):
        return False
    return True


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Read an env var and return a boolean, warning on unrecognized values.

    Truthy values: ``1``, ``true``, ``yes``, ``on``; falsy: ``0``/``false``/
    ``no``/``off`` (case-insensitive). Garbage logs a warning naming the
    variable and returns ``default``. Centralized so every SSRF /
    feature-flag check agrees on what "on" means.
    """
    return parse_bool_value(os.environ.get(name), default, env_name=name)


def _clamp(value, lo, hi):
    """Bound ``value`` to ``[lo, hi]`` when the bounds are provided."""
    if lo is not None and value < lo:
        return lo
    if hi is not None and value > hi:
        return hi
    return value


def get_mapping_int(
    mapping: Mapping[str, str], name: str, default: int, *, lo: int | None = None, hi: int | None = None
) -> int:
    """Mapping face of :func:`get_env_int`; identical never-raise contract.

    ``CrawlerSettings.from_mapping`` reads a mapping (os.environ in prod, a
    dict in tests), so the chokepoint must serve both faces from one core.
    """
    fallback = _clamp(_as_int_default(default, name=name), lo, hi)
    raw = mapping.get(name)
    if raw is None:
        return fallback
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "invalid_int_env", extra={"env_name": name, "env_value": raw, "fallback": fallback}
        )
        return fallback
    return _clamp(value, lo, hi)


def get_mapping_float(
    mapping: Mapping[str, str], name: str, default: float, *, lo: float | None = None, hi: float | None = None
) -> float:
    """Mapping face of :func:`get_env_float`; identical never-raise contract."""
    fallback = _clamp(_as_float_default(default, name=name), lo, hi)
    raw = mapping.get(name)
    if raw is None:
        return fallback
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            "invalid_float_env", extra={"env_name": name, "env_value": raw, "fallback": fallback}
        )
        return fallback
    if not math.isfinite(value):
        logging.getLogger(__name__).warning(
            "non_finite_float_env", extra={"env_name": name, "env_value": raw, "fallback": fallback}
        )
        return fallback
    return _clamp(value, lo, hi)


def parse_bool_value(raw: str | None, default: bool, *, env_name: str) -> bool:
    """Parse a boolean env value; warn (never silently flip) on garbage.

    Unset/empty -> ``default``. Recognized truthy: 1/true/yes/on; falsy:
    0/false/no/off (case-insensitive). Anything else logs a warning naming
    the variable and returns ``default`` -- so a typo can no longer silently
    fail a policy open (``RESPECT_ROBOTS=ture`` keeps robots respected).
    """
    if raw is None or not raw.strip():
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    logging.getLogger(__name__).warning(
        "invalid_bool_env for %s: %r (using default %r)",
        env_name,
        raw,
        default,
        extra={"env_name": env_name, "env_value": raw, "fallback": default},
    )
    return default


def _as_int_default(value, *, name) -> int:
    """Coerce a caller-supplied default to a finite int; never raise.

    Accepts int/float/int-like str; anything else (None, "abc", inf, nan)
    logs and yields 0 rather than leaking a non-numeric default that would
    turn later arithmetic (``limit // default``) into a crash or a
    string-repetition blow-up.
    """
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(value)
        return int(value)
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning("invalid_env_default", extra={"env_name": name, "fallback": 0})
        return 0


def _as_float_default(value, *, name) -> float:
    """Coerce a caller-supplied default to a finite float; never raise."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning("invalid_env_default", extra={"env_name": name, "fallback": 0.0})
        return 0.0
    if not math.isfinite(result):
        logging.getLogger(__name__).warning(
            "non_finite_env_default", extra={"env_name": name, "fallback": 0.0}
        )
        return 0.0
    return result


def get_env_int(name: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
    """Read an env var as an int; never crash, always return an int.

    Hardened against every class of bad input (adversarial review: the
    chokepoint must make the mistake unrepresentable): a typo'd env var or
    an un-coercible/non-numeric *default* falls back to a bounded int, and
    the result is always clamped to ``[lo, hi]`` when bounds are given. A
    string default like ``"0.5"`` can no longer leak a ``str`` into
    downstream arithmetic.
    """
    return get_mapping_int(os.environ, name, default, lo=lo, hi=hi)


def get_env_float(name: str, default: float, *, lo: float | None = None, hi: float | None = None) -> float:
    """Read an env var as a float; never crash, always a finite float.

    Rejects non-finite values (``inf``/``nan``) from BOTH the default and the
    environment: ``float("inf")`` parses cleanly but ``int(limit * inf)``
    overflows at import, so the value is treated as garbage and the bounded
    fallback is returned instead.
    """
    return get_mapping_float(os.environ, name, default, lo=lo, hi=hi)


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
        except Exception:
            # A poisoned/unparseable host falls through to the heuristic.
            pass
    return _heuristic_registrable_domain(host)


def public_url_or_none(raw: str | None, *, resolve_against: str | None = None) -> str | None:
    """Egress filter: resolve relatives FIRST, then strictly accept http/https.

    Attacker-influenced URLs leave the service through several doors
    (js_endpoints, next_page_url, sitemap locs, discovered links), each
    filtered slightly differently. The recurring inconsistency: a bare
    ``urlparse(url).scheme`` check treats protocol-relative (``//evil.com/x``)
    and relative (``/api``) URLs as scheme ``""`` -- one site allowed ``""``
    (emitting unresolvable URLs), another dropped it (killing relative
    pagination). Resolving against the source URL first makes both cases
    absolute, after which only http/https survive; ``javascript:``/``data:``/
    ``ftp:`` are always None, as is anything unresolvable.
    """
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    if resolve_against:
        candidate = urljoin(resolve_against, candidate)
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return candidate
