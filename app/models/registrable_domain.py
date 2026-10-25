"""What site does this URL belong to? URL identity, not host policy."""

from __future__ import annotations

import sys
from contextlib import suppress
from importlib import import_module
from typing import Protocol, cast

from app.models.address_class import AddressClass, classify

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


def registrable_domain(host: str, *, psl: _PublicSuffixDataset | None = None) -> str:
    """The registrable site a host belongs to, or the host itself when no site."""
    host = host.lower().rstrip(".")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except Exception:
        ascii_host = host
    if psl is None:
        psl = _public_suffix_list()
    # An IP literal has no registrable site: the PSL '*' default would map it to
    # its last two octets, colliding unrelated hosts on one site key (#527).
    if classify(ascii_host) is not AddressClass.NOT_AN_ADDRESS:
        return host
    if psl is not None:
        with suppress(Exception):
            registrable = psl.privatesuffix(ascii_host)
            if registrable:
                return registrable
    return _heuristic_registrable_domain(host)


def is_public_suffix(host: str, *, psl: _PublicSuffixDataset | None = None) -> bool:
    """Whether ``host`` is itself a public suffix, with no registrable site under it.

    The cookie admission edge refuses a ``Domain=`` naming one of these (#438):
    ``com`` or ``co.uk`` would otherwise domain-match every host under the suffix.
    """
    host = host.lower().rstrip(".")
    if not host:
        return False
    if "." not in host:
        # Every single label is a TLD under the PSL's wildcard rule.
        return True
    if psl is None:
        psl = _public_suffix_list()
    if psl is not None:
        with suppress(Exception):
            return psl.privatesuffix(host) is None
    return host in TWO_PART_PUBLIC_SUFFIXES


__all__ = ["TWO_PART_PUBLIC_SUFFIXES", "is_public_suffix", "registrable_domain"]
