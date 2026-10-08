"""Shared robots.txt policy loader, cache, parser adapter, and matcher."""

from __future__ import annotations

import asyncio
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from app.models.host_policy import HostPolicy
from app.models.http_fetcher import HttpFetcher
from app.models.normalized_url import NormalizedUrl
from app.orchestration.fetch_helpers import _single_flight
from app.orchestration.fetch_helpers import fetch_and_decode as default_fetch_and_decode
from app.orchestration.robots_parse import (
    parse_robots_crawl_delay,
    parse_robots_rules,
    parse_robots_sitemaps,
)
from app.safety.bounded_ttl import BoundedTTLCache

ROBOTS_CACHE_MAX_ENTRIES = 2048
ROBOTS_CACHE_TTL_SECONDS = 600.0
ROBOTS_FAILURE_TTL_SECONDS = 60.0
type RobotsRules = tuple[list[tuple[bool, str]], list[str], float | None]
FetchAndDecode = Callable[
    [HttpFetcher, NormalizedUrl, dict[str, str], int], Awaitable[tuple[str | None, int | None]]
]

robots_cache: BoundedTTLCache[tuple[str, HostPolicy], RobotsRules] = BoundedTTLCache(
    max_entries=ROBOTS_CACHE_MAX_ENTRIES,
    ttl=ROBOTS_CACHE_TTL_SECONDS,
)
_inflight: dict[tuple[str, HostPolicy], asyncio.Task[RobotsRules]] = {}


async def _load_uncached(
    target: NormalizedUrl,
    fetcher: HttpFetcher,
    timeout_seconds: int,
    fetch_and_decode: FetchAndDecode,
    policy: HostPolicy,
) -> RobotsRules:
    # ``resolve`` is the urljoin-and-admit pair, so the robots document's URL
    # is an admitted value by construction. None -- a same-origin absolute
    # path on an admitted page, so unreachable -- falls through to the
    # failure arm below and fails closed like any unfetchable robots.txt.
    robots_url = target.resolve("/robots.txt")
    body, status = (
        (None, None)
        if robots_url is None
        else await fetch_and_decode(fetcher, robots_url, {}, timeout_seconds)
    )
    if status == 200 and body is not None:
        value: RobotsRules = (
            parse_robots_rules(body),
            [loc for loc in parse_robots_sitemaps(body) if target.resolve(loc) is not None],
            parse_robots_crawl_delay(body),
        )
        ttl = ROBOTS_CACHE_TTL_SECONDS
    elif status == 404:
        value = ([], [], None)
        ttl = ROBOTS_CACHE_TTL_SECONDS
    else:
        value = ([(False, "/")], [], None)
        ttl = ROBOTS_FAILURE_TTL_SECONDS
    robots_cache.put((target.origin, policy), value, ttl=ttl)
    return value


async def load_robots_rules(
    target: NormalizedUrl,
    fetcher: HttpFetcher,
    timeout_seconds: int,
    *,
    fetch_and_decode: FetchAndDecode | None = None,
    policy: HostPolicy,
) -> RobotsRules:
    """Load one origin's robots policy through the process-wide cache.

    The page is the value the caller already holds; the origin the rules are
    scoped to -- and the text the cache is keyed by -- is derived, not passed.
    """
    cache_key = (target.origin, policy)
    cached = robots_cache.get(cache_key)
    if cached is not None:
        return cached
    decode = default_fetch_and_decode if fetch_and_decode is None else fetch_and_decode
    return await _single_flight(
        _inflight,
        cache_key,
        lambda: _load_uncached(target, fetcher, timeout_seconds, decode, policy),
    )


def cached_sitemaps(target: NormalizedUrl, *, policy: HostPolicy) -> list[str] | None:
    cached = robots_cache.get((target.origin, policy))
    return None if cached is None else list(cached[1])


class _RobotsPathMatcher:
    __slots__ = ("_literals", "_anchored_end")

    def __init__(self, pattern: str) -> None:
        self._anchored_end = pattern.endswith("$") and len(pattern) > 1
        body = pattern[:-1] if self._anchored_end else pattern
        self._literals = body.split("*")

    def match(self, path: str) -> bool:
        literals = self._literals
        first = literals[0]
        if not path.startswith(first):
            return False
        pos = len(first)
        if self._anchored_end:
            rest = literals[1:]
            if not rest:
                return pos == len(path)
            last = rest[-1]
            end_start = len(path) - len(last)
            if end_start < pos:
                return False
            scan = pos
            for literal in rest[:-1]:
                found = path.find(literal, scan)
                if found == -1 or found + len(literal) > end_start:
                    return False
                scan = found + len(literal)
            return path.endswith(last)
        for literal in literals[1:]:
            found = path.find(literal, pos)
            if found == -1:
                return False
            pos = found + len(literal)
        return True


class _MatchAll:
    __slots__ = ()

    def match(self, path: str) -> bool:
        return True


class _PrefixMatcher:
    __slots__ = ("_prefix",)

    def __init__(self, pattern: str) -> None:
        self._prefix = pattern

    def match(self, path: str) -> bool:
        return path.startswith(self._prefix)


ROBOTS_PATTERN_CACHE_PER_HOST = 1024
ROBOTS_PATTERN_CACHE_MAX_HOSTS = 1024
type RobotsMatcher = re.Pattern[str] | _RobotsPathMatcher | _MatchAll | _PrefixMatcher


class _RobotsPatternCache:
    __slots__ = ("_hosts", "_lock")

    def __init__(self) -> None:
        self._hosts: OrderedDict[str, OrderedDict[str, RobotsMatcher]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, host: str, pattern: str) -> RobotsMatcher:
        with self._lock:
            table = self._hosts.get(host)
            if table is None:
                table = OrderedDict()
                self._hosts[host] = table
                while len(self._hosts) > ROBOTS_PATTERN_CACHE_MAX_HOSTS:
                    self._hosts.popitem(last=False)
            else:
                self._hosts.move_to_end(host)
            cached = table.get(pattern)
            if cached is not None:
                table.move_to_end(pattern)
                return cached
            matcher = _compile_robots_pattern(pattern)
            table[pattern] = matcher
            while len(table) > ROBOTS_PATTERN_CACHE_PER_HOST:
                table.popitem(last=False)
            return matcher


_robots_pattern_cache = _RobotsPatternCache()


def _compile_robots_pattern(pattern: str) -> RobotsMatcher:
    if pattern == "":
        return _MatchAll()
    if "*" not in pattern and not (pattern.endswith("$") and len(pattern) > 1):
        return _PrefixMatcher(pattern)
    return _RobotsPathMatcher(pattern)


def _robots_pattern_to_regex(host: str, pattern: str) -> RobotsMatcher:
    return _robots_pattern_cache.get(host, pattern)


def robots_allows(target_url: NormalizedUrl, rules: list[tuple[bool, str]]) -> bool:
    path = target_url.path or "/"
    if target_url.query:
        path = f"{path}?{target_url.query}"
    host = target_url.host
    # No allow-all shortcut for the empty `Disallow:` form. RFC 9309 2.2.2 makes
    # an empty value a rule to IGNORE, not a verdict, so it is skipped with the
    # other unmatched patterns below like any rule that cannot apply. Scanning
    # for it up front and returning True on sight read that ignore as a verdict
    # for the whole group: a file declaring `Disallow:` next to
    # `Disallow: /private` -- both legal in one block -- allowed everything,
    # including the path it had just named. A group whose ONLY rule is the empty
    # form still allows everything, because the loop starts allowed and skips it.
    allowed = True
    best_len = -1
    for is_allow, pattern in rules:
        if not pattern:
            continue
        if _robots_pattern_to_regex(host, pattern).match(path):
            plen = len(pattern)
            if plen > best_len:
                best_len = plen
                allowed = is_allow
            elif plen == best_len and is_allow:
                allowed = True
    return allowed


__all__ = [
    "ROBOTS_CACHE_MAX_ENTRIES",
    "ROBOTS_CACHE_TTL_SECONDS",
    "ROBOTS_FAILURE_TTL_SECONDS",
    "ROBOTS_PATTERN_CACHE_MAX_HOSTS",
    "ROBOTS_PATTERN_CACHE_PER_HOST",
    "FetchAndDecode",
    "RobotsMatcher",
    "RobotsRules",
    "cached_sitemaps",
    "load_robots_rules",
    "robots_allows",
    "robots_cache",
]
