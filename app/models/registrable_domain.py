"""What site does this URL belong to? URL identity, not host policy."""

from __future__ import annotations

import sys
from contextlib import suppress
from importlib import import_module
from typing import Protocol, cast
from urllib.parse import urlparse

TWO_PART_PUBLIC_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au"}


class _PublicSuffixDataset(Protocol):
    def privatesuffix(self, domain: str) -> str | None: ...


# ``_PSL_UNSET`` keeps "never attempted" distinct from "attempted and
# unavailable": the accessor must keep trying in the first, never again in the second.
_PSL_UNSET = None
_PSL: _PublicSuffixDataset | None = _PSL_UNSET


def _public_suffix_list() -> _PublicSuffixDataset | None:
    """Lazily load the publicsuffixlist snapshot, None when absent.

    Failure falls back to the heuristic rather than failing an import or a gate.
    """
    global _PSL
    # Read through the module object so a caller that deletes the attribute --
    # the test seam for "never loaded" -- is read as uninitialised, not a NameError.
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
    """The registrable site a URL belongs to, or its host when there is no site."""
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


__all__ = ["TWO_PART_PUBLIC_SUFFIXES", "registrable_domain"]
