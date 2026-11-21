"""Test-only host assertion: exact canonical-host membership, never substring.

CodeQL flags in/startswith on URL text; these are behavioral assertions, not
gates, but they are imprecise. Route every URL assertion through this helper.
"""

from __future__ import annotations

import ast
import re
from importlib import import_module
from typing import Protocol, cast
from urllib.parse import urlparse

from tests._walk_primitives import string_constants


class _SuffixSet(Protocol):
    def privatesuffix(self, domain: str) -> str | None: ...


_PSL_MODULE = import_module("publicsuffixlist")
_ICANN_SUFFIXES = cast(_SuffixSet, _PSL_MODULE.PublicSuffixList(accept_unknown=False, only_icann=True))
_FILE_SUFFIXES = frozenset(
    {"py", "md", "txt", "json", "yml", "yaml", "toml", "cfg", "ini", "log", "csv", "xml", "html", "rst"}
)


def host_of(value) -> str:
    """Canonical host of ``value``: a ``NormalizedUrl``, an ``httpx.URL``, or a
    URL/host string. Plain host strings pass through unchanged."""
    host = getattr(value, "host", None)
    if host is not None:
        return str(host)
    if isinstance(value, str):
        parsed = urlparse(value if "://" in value else f"//{value}")
        return parsed.hostname or value.split("/", 1)[0].split(":", 1)[0]
    return str(value)


def host_in(container, host: str) -> bool:
    """True when some item in ``container`` has canonical host ``host``.

    Exact host equality -- ``example.com`` never matches ``notexample.com``."""
    return any(host_of(item) == host for item in container)


def looks_like_url(value: str) -> bool:
    """True for an absolute URL or a DNS name under a known public suffix."""
    if "://" in value:
        return True
    try:
        host = host_of(value)
    except ValueError:
        return False
    labels = host.split(".")
    return (
        len(labels) > 1
        and all(label and all(char.isalnum() or char == "-" for char in label) for label in labels)
        and _ICANN_SUFFIXES.privatesuffix(host) is not None
    )


def is_host_literal(value: str) -> bool:
    """True for a bare host: the substring-sanitization shape CodeQL flags."""
    if "://" in value or "/" in value or not looks_like_url(value):
        return False
    if ":" in value and value.rsplit(":", 1)[-1].isdigit():
        return False
    labels = host_of(value).split(".")
    return labels[-1] not in _FILE_SUFFIXES and len(labels[0]) > 1


def host_in_source(source: str, host: str) -> bool:
    """True when a Python source string literal names ``host`` exactly."""
    return any(host_of(value) == host for _, value in string_constants(ast.parse(source)))


def host_in_text(text: str, host: str) -> bool:
    """True when a host appears in arbitrary text; exact host, never substring."""
    return any(host_of(token) == host for token in re.split(r"[\s\"'()\[\],<>;]+", text) if token)
