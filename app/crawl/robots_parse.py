"""Single robots.txt line-scanner and group selector (jscpd #5).

The engine parsed one robots.txt body with three hand-rolled scanners
(crawl-delay at module level, rules + sitemaps as nested closures), each
re-implementing the same BOM strip → comment strip → ``partition(":")``
walk plus, for the two group parsers, the same user-agent accumulator and
RFC 9309 most-specific-group selection. The copies had already drifted on
one gate (an empty ``Crawl-delay:`` must not open a group while an empty
``Disallow:`` must, because it means allow-all).

The family is linear-time by construction (#1766): a str.find/partition
walk, never a regex over attacker-controlled text. ``accepts`` is the
per-parser policy of *which directives it consumes*; everything structural
is shared here so a robots-parsing fix lands once.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from typing import Any

from app.constants import USER_AGENT_TOKEN


def iter_robots_fields(text: str) -> Iterator[tuple[str, str]]:
    """Yield ``(key, value)`` per meaningful robots line, key case-folded.

    Strips a leading BOM and trailing ``#`` comments, drops blank lines and
    colon-less lines (inert under every directive grammar), and splits at
    the first colon only so values may contain ``:`` (``Sitemap: https://…``).
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
    """Tokenize into ``(agents, directives)`` user-agent groups.

    ``accepts(key, value)`` decides whether a line is *this parser's*
    directive; only an accepted line materializes a group. Agents that
    declare nothing consumed therefore contribute no group of their own —
    foreign blocks stay out of selection through :func:`select_robots_values`
    matching on the product token. (Agent lists accumulate across
    non-consumed lines, exactly as both old scanners behaved.)
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
    groups: list[tuple[tuple[str, ...], list[Any]]], token: str = USER_AGENT_TOKEN
) -> tuple[list[Any] | None, list[Any] | None]:
    """Return ``(matched, star)``: the most-specific group whose agent is a
    substring of our product token (longest agent wins), and the first
    ``*`` group.

    Matching is against the product token, not the full User-Agent string —
    URL/version fragments in a robots group must not claim our crawler.
    """
    best: list[Any] | None = None
    best_len = -1
    star: list[Any] | None = None
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
    """``(is_allow, path)`` pairs for the group governing this crawler.

    A matched group is final even when its content is the empty-path
    ``Disallow:`` the matcher reads as allow-all — an answer, not an
    absence, so it never falls through to the star group.
    """
    groups = robots_groups(text, lambda key, value: key in ("allow", "disallow"))
    best, star = select_robots_values(groups, token)
    values = best if best is not None else (star or [])
    return [(key == "allow", value) for key, value in values]


def parse_robots_crawl_delay(text: str, *, token: str = USER_AGENT_TOKEN) -> float | None:
    """Seconds from the governing group's last valid ``Crawl-delay``.

    An empty value never opens a group (the gate the rules parser does not
    share), non-numeric, negative and non-finite values are dropped, and the
    last valid value in a group wins — matching the old overwrite-on-valid
    scan. A token-matched group whose values are all invalid yields no delay
    and falls back to the star group, exactly as the old ``best_delay is None
    → star_delay`` return did. Non-finite numbers (``inf``/``nan``, or a
    value that overflows to ``inf``) are rejected here so a hostile robots.txt
    can never install an unbounded politeness floor; the scheduler clamps
    finite values to the politeness ceiling at the write.
    """
    groups = robots_groups(text, lambda key, value: key == "crawl-delay" and value != "")
    best, star = select_robots_values(groups, token)

    def _last_valid(values: list[Any] | None) -> float | None:
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
