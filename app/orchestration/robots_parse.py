"""One robots.txt line-scanner and group selector, shared by all parsers: the
family is linear-time (a partition walk, never a regex over attacker text),
``accepts`` the per-parser policy, and structure lands here so a fix lands once.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator

from app.vocabulary.identity import USER_AGENT_TOKEN

#: One robot's consumed directives as ``(key, value)`` pairs.
_Directives = list[tuple[str, str]]


def iter_robots_fields(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(key, value)`` per meaningful robots line, key case-folded.

    Strips a leading BOM and trailing ``#`` comments, drops blank and colon-less
    lines, and splits at the first colon so values may contain ``:``.
    """
    for raw in text.lstrip("﻿").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        yield key.strip().lower(), value.strip()


def robots_groups(
    text: str, accepts: Callable[[str, str], bool]
) -> list[tuple[tuple[str, ...], list[tuple[str, str]]]]:
    """Tokenize into ``(agents, directives)`` user-agent groups; only a line
    ``accepts`` materializes a group, and agents accumulate across non-consumed
    lines (foreign blocks stay out via :func:`select_robots_values`).
    """
    groups: list[tuple[tuple[str, ...], list[tuple[str, str]]]] = []
    last_agents: list[str] = []
    current: tuple[tuple[str, ...], list[tuple[str, str]]] | None = None
    for key, value in iter_robots_fields(text):
        if key == "user-agent":
            if current is not None:
                groups.append(current)
                current = None
            last_agents.append(value.lower())
        elif accepts(key, value):
            if current is None:
                current = (tuple(last_agents), [])
                last_agents = []
            current[1].append((key, value))
    if current is not None:
        groups.append(current)
    return groups


def select_robots_values(
    groups: list[tuple[tuple[str, ...], _Directives]], token: str = USER_AGENT_TOKEN
) -> tuple[_Directives | None, _Directives | None]:
    """Return ``(matched, star)``: the most-specific group (longest agent that is
    a substring of the product token) and the first ``*`` group. Both sides are
    lower-cased here, or a mixed-case token would match nothing and fall to star.
    """
    token = token.lower()
    best: _Directives | None = None
    best_len = -1
    star: _Directives | None = None
    for agents, values in groups:
        for agent in agents:
            if agent == "*":
                if star is None:
                    star = values
                break
            if agent and agent in token and len(agent) > best_len:
                best_len = len(agent)
                best = values
                break
    return best, star


def parse_robots_rules(text: str, *, token: str = USER_AGENT_TOKEN) -> list[tuple[bool, str]]:
    """Return ``(is_allow, path)`` pairs for the group governing this crawler.

    A matched group is final even when empty-path ``Disallow:`` (an answer, not
    an absence); an empty pattern is passed through for the matcher to ignore.
    """
    groups = robots_groups(text, lambda key, value: key in ("allow", "disallow"))
    best, star = select_robots_values(groups, token)
    values = best if best is not None else (star or [])
    return [(key == "allow", value) for key, value in values]


def parse_robots_crawl_delay(text: str, *, token: str = USER_AGENT_TOKEN) -> float | None:
    """Seconds from the governing group's last valid ``Crawl-delay``, else the
    star group's. Empty never opens a group; non-numeric, negative and non-finite
    values are dropped, so a hostile file cannot install an unbounded floor.
    """
    groups = robots_groups(text, lambda key, value: key == "crawl-delay" and value != "")
    best, star = select_robots_values(groups, token)

    def _last_valid(values: _Directives | None) -> float | None:
        delay: float | None = None
        for _, raw in values or []:
            try:
                parsed = float(raw)
            except ValueError:
                continue
            if parsed >= 0 and math.isfinite(parsed):
                delay = parsed
        return delay

    if best is not None:
        matched_delay = _last_valid(best)
        if matched_delay is not None:
            return matched_delay
    return _last_valid(star)


def parse_robots_sitemaps(text: str) -> list[str]:
    """All ``Sitemap:`` locations, in document order (grouping is irrelevant)."""
    return [value for key, value in iter_robots_fields(text) if key == "sitemap" and value]


__all__ = [
    "iter_robots_fields",
    "parse_robots_crawl_delay",
    "parse_robots_rules",
    "parse_robots_sitemaps",
    "robots_groups",
    "select_robots_values",
]
