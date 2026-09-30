from __future__ import annotations

import logging
import math
import os
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import lru_cache, partial
from importlib import import_module
from typing import Protocol, TypeVar, cast
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.addresses import AddressClass, classify
from app.models import NormalizedUrl


class _PublicSuffixDataset(Protocol):
    def privatesuffix(self, domain: str) -> str | None: ...


PRIVATE_HOSTS = {"localhost", "host.docker.internal"}
TWO_PART_PUBLIC_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au"}


def _normalize_policy_host(host: str) -> str:
    value = host.strip().lower().rstrip(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return value


def _policy_host_entry(entry: str) -> str:
    value = entry.strip()
    if not value:
        return ""
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = parsed.hostname or value.split("/", 1)[0].split(":", 1)[0]
    if host.startswith("*."):
        host = host[2:]
    return _normalize_policy_host(host)


@lru_cache(maxsize=256)
def _policy_hosts(raw: str) -> tuple[str, ...]:
    return tuple(entry for item in raw.split(",") if (entry := _policy_host_entry(item)))


@dataclass(frozen=True)
class HostPolicy:
    allowed_hosts: tuple[str, ...]
    denied_hosts: tuple[str, ...]

    @classmethod
    def unrestricted(cls) -> HostPolicy:
        return cls((), ())

    @classmethod
    def from_strings(cls, allowed_hosts: str, denied_hosts: str) -> HostPolicy:
        return cls(_policy_hosts(allowed_hosts), _policy_hosts(denied_hosts))

    @classmethod
    def from_settings(cls, settings: object) -> HostPolicy:
        return cls.from_strings(
            str(getattr(settings, "allowed_hosts", "") or ""),
            str(getattr(settings, "denied_hosts", "") or ""),
        )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_hosts",
            tuple(_policy_host_entry(entry) for entry in self.allowed_hosts if _policy_host_entry(entry)),
        )
        object.__setattr__(
            self,
            "denied_hosts",
            tuple(_policy_host_entry(entry) for entry in self.denied_hosts if _policy_host_entry(entry)),
        )


HOST_POLICY_ENTRY_POINTS = (
    "app/api/crawl_endpoint.py:crawl",
    "app/api/crawl_executor.py:execute_crawl",
    "app/api/jobs_router.py:create_job",
    "app/api/map_endpoint.py:map_site",
    "app/api/render.py:render_request",
    "app/api/async_crawl_runner.py:run_async_crawl",
    "app/api/response_shaping.py:frontier_link_url",
    "app/api/webhooks.py:post_webhook",
    "app/api/webhooks.py:deliver_pinned_webhook",
    "app/mcp/tools.py:_handle_crawl",
    "app/mcp/tools.py:_handle_map",
    "app/mcp/tools.py:_handle_render",
    "app/mcp/tools.py:_handle_search",
)


def _host_policy_reason(host: str, *, policy: HostPolicy) -> str | None:
    normalized = _normalize_policy_host(host)
    if any(normalized == entry or normalized.endswith(f".{entry}") for entry in policy.denied_hosts):
        return "host_denied"
    if policy.allowed_hosts and not any(
        normalized == entry or normalized.endswith(f".{entry}") for entry in policy.allowed_hosts
    ):
        return "host_not_allowed"
    return None


# Lazily-initialised public-suffix list. ``publicsuffixlist`` is an optional
# dependency, and importing it (and building the table) is only worth paying
# for when a URL actually needs its suffix resolved. ``_PSL_UNSET`` is the
# sentinel that keeps "never attempted" distinct from "attempted and
# unavailable" -- the accessor must keep trying in the first case and must
# never re-attempt in the second, so a plain ``None`` cannot serve both.
_PSL_UNSET = None
_PSL: _PublicSuffixDataset | None = _PSL_UNSET

# Attribution/tracking query parameters that carry no content-identity
# information. Dropping them lets URLs that differ only by UTM/tracking tags
# canonicalize to the same key, preventing duplicate fetches of the same page
# Bare ``ref`` is intentionally excluded: it is ambiguous and frequently
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


def normalize_public_http_url(url: str, *, policy: HostPolicy) -> NormalizedUrl:
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
    host_policy_reason = _host_policy_reason(host, policy=policy)
    if host_policy_reason:
        raise ValueError(host_policy_reason)
    # Local dev/test escape hatch: the crawler is a localhost dev service and
    # tests run a real in-process server on loopback. Allow opting out of the
    # private-network guard via env var rather than relaxing it by default.
    # Inverted-by-default: any value not explicitly recognized as truthy keeps
    # the SSRF guard ON, using the shared ``parse_bool_value`` policy so every
    # path agrees on what counts as "on". Both layers below are memoized
    # because this function runs per anchor: the env decision
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
        # as duplicate pages. Remaining params are sorted so URLs differing
        # only in parameter order also canonicalize identically.
        pairs = [(k, v) for (k, v) in parse_qsl(query, keep_blank_values=True) if not _is_tracking_param(k)]
        pairs.sort()
        query = urlencode(pairs, doseq=True)
    # The construction point of the type: everything upstream is raw
    # input; everything downstream that names this shape is typed.
    return NormalizedUrl(urlunparse((parsed.scheme, netloc, path, parsed.params, query, "")))


def _strip_trailing_colon(value: str) -> str:
    return value[:-1] if value.endswith(":") else value


# (raw env value, parsed decision) for CRAWLUTRON_ALLOW_PRIVATE_TARGETS.
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
    raw = os.environ.get("CRAWLUTRON_ALLOW_PRIVATE_TARGETS")
    cached = _PRIVATE_TARGET_DECISION
    if cached is not None and cached[0] == raw:
        return cached[1]
    decision = parse_bool_env("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", default=False)
    _PRIVATE_TARGET_DECISION = (raw, decision)
    if decision:
        # Warned at the decision, not at each call site: this runs on the
        # per-fetch SSRF path, so a warning here would be a log flood. The memo
        # makes it once per distinct env value, so a deployment that sets the
        # variable logs exactly once and a test that re-sets it still sees it.
        logging.getLogger(__name__).warning(
            "SSRF private-target guard DISABLED by CRAWLUTRON_ALLOW_PRIVATE_TARGETS=%r: "
            "caller-supplied URLs may now reach private, loopback, and link-local "
            "addresses, including cloud metadata endpoints. Unset it, or set it to 0, "
            "outside local development.",
            raw,
            extra={"env_name": "CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "env_value": raw},
        )
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
    # A host that is not an address at all is a name, and what it resolves to
    # is judged separately against ``_is_public_ip_value`` once the resolver
    # has answered. Only a literal that lands in a rejected range is refused.
    address_class = classify(host)
    return address_class in (AddressClass.NOT_AN_ADDRESS, AddressClass.PUBLIC)


def _is_public_ip_value(value: str) -> bool:
    """Return whether a host string is safe for public egress.

    Unlike ``_host_is_public`` this is asked of a *resolved* address, so a
    string that is not an address at all is not public here.
    """
    return classify(value) is AddressClass.PUBLIC


def parse_bool_env(name: str, default: bool = False) -> bool:
    """Read an env var and return a boolean, warning on unrecognized values.

    Truthy values: ``1``, ``true``, ``yes``, ``on``; falsy: ``0``/``false``/
    ``no``/``off`` (case-insensitive). Surrounding whitespace is ignored.
    Garbage logs a warning naming the variable and returns ``default``.
    Centralized so every SSRF /
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


T = TypeVar("T", int, float)


def _finite_or_none(value: float) -> float | None:
    """A finite float, or None when the parsed value cannot be honored."""
    return value if math.isfinite(value) else None


def in_range(lo: T, hi: T) -> Callable[[T], T | None]:
    """A validator that refuses a value outside ``[lo, hi]`` instead of clamping it.

    ``_mapping_number``'s own ``lo``/``hi`` clamp, which is right for a tuning
    knob and wrong for a value that is part of a contract: a deployment whose
    ``DEFAULT_MAX_RETRIES`` asks for 9 and silently gets 5 cannot tell anyone,
    and the code that clamped is a chokepoint, so the whole deployment is
    affected. Fed to ``validate=`` this warns and returns the fallback, so the
    misconfiguration is visible at startup instead of at request time.

    The clamp stays for the caller's own fallback value; this guards only what
    the environment said.
    """

    def within(value: T) -> T | None:
        return value if lo <= value <= hi else None

    return within


def _mapping_number(
    mapping: Mapping[str, str],
    name: str,
    fallback: T,
    *,
    parse: Callable[[str], T],
    invalid_event: str,
    validate: Callable[[T], T | None] | None = None,
    validate_event: str | None = None,
    lo: T | None = None,
    hi: T | None = None,
) -> T:
    """The one never-raise numeric env read shared by the int and float faces.

    Both public readers differed only in the parse callable, the warning event
    and the float-only finiteness check. Restating that body per type is how
    the two faces drifted, so the shape lives here once and each face supplies
    only what genuinely differs.
    """
    raw = mapping.get(name)
    if raw is None:
        return fallback
    try:
        value = parse(str(raw).strip())
    except (TypeError, ValueError, OverflowError):
        logging.getLogger(__name__).warning(
            invalid_event, extra={"env_name": name, "env_value": raw, "fallback": fallback}
        )
        return fallback
    if validate is not None and validate_event is not None:
        checked = validate(value)
        if checked is None:
            logging.getLogger(__name__).warning(
                validate_event, extra={"env_name": name, "env_value": raw, "fallback": fallback}
            )
            return fallback
        value = checked
    return _clamp(value, lo, hi)


def get_mapping_int(
    mapping: Mapping[str, str],
    name: str,
    # The set a config default can actually be, not `int`. A default arrives
    # from a settings mapping or an environment string, so it is a number, a
    # string, or nothing -- and an unparseable one is a documented input, which
    # is why `_as_int_default` is unannotated and enumerates the junk it absorbs
    # (None, "abc", inf, nan). `int` said the opposite of the body and of the
    # docstring three lines below. Coercion stays total in the body, so a
    # wrongly-typed default is still absorbed rather than raised; this says what
    # the edge promises, not that it is defensively unreachable.
    default: int | float | str | None,
    *,
    lo: int | None = None,
    hi: int | None = None,
    validate: Callable[[int], int | None] | None = None,
    validate_event: str | None = None,
) -> int:
    """Read an integer from a mapping, or from the environment; never crash,
    always return an int.

    Both faces are ONE function: :func:`get_env_int` is this function bound
    to ``os.environ``. The mapping form is the real ``def`` because
    ``CrawlerSettings.from_mapping`` reads a mapping (os.environ in prod, a
    dict in tests), and the env form used to be a second copy of this
    signature -- a second place to forget an option, where forgetting is
    silent. (Adding ``validate``/``validate_event`` here is what exposed that
    restatement: the duplication gate caught the parameter list twice.)

    Hardened against every class of bad input: the
    chokepoint must make the mistake unrepresentable. A typo'd env var or
    an un-coercible/non-numeric *default* falls back to a bounded int, and
    the result is always clamped to ``[lo, hi]`` when bounds are given. A
    string default like ``"0.5"`` can no longer leak a ``str`` into
    downstream arithmetic.

    ``validate``/``validate_event`` exist here for the same reason they exist on
    the float face: they are the way a value that is part of a contract gets
    REJECTED (warned, and replaced by the fallback) rather than clamped into a
    different number. The int face could not say that before, so every
    out-of-range integer setting was rewritten in silence.
    """
    return _mapping_number(
        mapping,
        name,
        _clamp(_as_int_default(default, name=name), lo, hi),
        parse=int,
        invalid_event="invalid_int_env",
        lo=lo,
        hi=hi,
        validate=validate,
        validate_event=validate_event,
    )


def get_mapping_float(
    mapping: Mapping[str, str],
    name: str,
    # The same real set as the int face, for the same reason.
    default: int | float | str | None,
    *,
    lo: float | None = None,
    hi: float | None = None,
) -> float:
    """Read a float from a mapping, or from the environment; never crash,
    always a finite float.

    Both faces are ONE function: :func:`get_env_float` is this function bound
    to ``os.environ``, for the reason given on the int face above.

    Rejects non-finite values (``inf``/``nan``) from BOTH the default and the
    environment: ``float("inf")`` parses cleanly but ``int(limit * inf)``
    overflows at import, so the value is treated as garbage and the bounded
    fallback is returned instead.
    """
    return _mapping_number(
        mapping,
        name,
        _clamp(_as_float_default(default, name=name), lo, hi),
        parse=float,
        invalid_event="invalid_float_env",
        validate=_finite_or_none,
        validate_event="non_finite_float_env",
        lo=lo,
        hi=hi,
    )


def parse_bool_value(raw: str | None, default: bool, *, env_name: str) -> bool:
    """Parse a boolean env value; warn (never silently flip) on garbage.

    Unset/empty -> ``default``. Recognized truthy: 1/true/yes/on; falsy:
    0/false/no/off (case-insensitive). Surrounding whitespace is ignored.
    Anything else logs a warning naming the variable and returns ``default``
    -- so a typo can no longer silently
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


# The env faces are the mapping faces bound to this process's environment,
# not a second copy of their signatures. An alias is the whole point: the cost
# is that `get_env_int.__doc__` is the mapping face's docstring (which is
# where the contract now lives anyway), and the win is that a new option is
# added in one place. It was two, and the second copy was already stale.
get_env_int = partial(get_mapping_int, os.environ)
get_env_float = partial(get_mapping_float, os.environ)


def _public_suffix_list():
    """Lazily load the real public-suffix dataset, None when absent.

    The publicsuffixlist package ships a snapshot .dat (no runtime network).
    Cached on first use; any failure falls back to the heuristic below so
    the service never fails to import or gate without the dataset.
    """
    global _PSL
    # Read through the module object (not the bare name) so a caller that
    # deletes the module attribute -- the test seam for "dataset was never
    # loaded" -- is treated as uninitialised rather than raising NameError.
    if getattr(sys.modules[__name__], "_PSL", _PSL_UNSET) is not _PSL_UNSET:
        return _PSL
    try:
        _PSL = cast(_PublicSuffixDataset, import_module("publicsuffixlist").PublicSuffixList())
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
        with suppress(Exception):
            registrable = psl.privatesuffix(ascii_host)
            if registrable:
                return registrable
    return _heuristic_registrable_domain(host)


def public_url_or_none(
    raw: str | None,
    *,
    resolve_against: str | None = None,
    policy: HostPolicy,
) -> str | None:
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
    normalized = safe_public_url_or_none(candidate, policy=policy)
    return str(normalized) if normalized is not None else None


def filter_js_endpoints(
    endpoints: list[dict[str, str]],
    *,
    resolve_against: str | None = None,
    policy: HostPolicy,
) -> list[dict[str, str]]:
    """Egress filter for the JS-endpoint discovery door.

    ``extract_js_endpoints`` scrapes URLs out of attacker-controlled HTML/JS
    and hands back ``{"url": ...}`` dicts. Two builders (HTTP and browser)
    each filtered that list with the same inline comprehension -- the shape
    that drifts the moment a third consumer forgets the copy. This is the
    single filter for the door: an endpoint survives only if its URL passes
    :func:`public_url_or_none` (resolve relatives first, then accept only
    http(s)), and the surviving endpoint carries the filtered URL so callers
    never see the raw scraped string.
    """
    return [
        {**endpoint, "url": clean}
        for endpoint in endpoints
        if (
            clean := public_url_or_none(
                str(endpoint.get("url", "")),
                resolve_against=resolve_against,
                policy=policy,
            )
        )
        is not None
    ]


def safe_public_url_or_none(raw: str | None, *, policy: HostPolicy) -> NormalizedUrl | None:
    """The single normalize-or-None chokepoint for URL egress doors.

    ``normalize_public_http_url`` raises ``ValueError`` on anything it rejects
    (private networks under SSRF policy, non-http schemes, un-normalizable
    input). Callers on egress doors -- search results, map links, batch dedupe,
    sitemap seeds -- all want the same thing: the canonical public URL, or
    ``None`` to drop the entry. Each one hand-rolled the same
    ``try/except ValueError: return None``, which is how a new door ships
    without the SSRF policy: the wrapper is easy to forget and easy to get
    subtly wrong, and nothing tests the absence.

    ``resolve_against`` is intentionally absent: callers that must resolve a
    relative URL first do so via :func:`public_url_or_none`, then pass the
    result here. Combining the two would make this function decide between
    two egress contracts, and the policy must apply identically at every door.
    """
    try:
        if raw is None:
            return None
        return normalize_public_http_url(raw, policy=policy)
    except ValueError:
        return None


__all__ = [
    "HOST_POLICY_ENTRY_POINTS",
    "PRIVATE_HOSTS",
    "TWO_PART_PUBLIC_SUFFIXES",
    "HostPolicy",
    "T",
    "filter_js_endpoints",
    "get_env_float",
    "get_env_int",
    "get_mapping_float",
    "get_mapping_int",
    "in_range",
    "normalize_public_http_url",
    "parse_bool_env",
    "parse_bool_value",
    "public_url_or_none",
    "registrable_domain",
    "safe_public_url_or_none",
]
