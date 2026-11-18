"""What site does this URL belong to? URL identity, not host policy."""

from __future__ import annotations

from importlib import import_module
from typing import Protocol, cast

from safe_url.address_class import AddressClass, classify
from safe_url.host_canonical import idna_host


class _PublicSuffixDataset(Protocol):
    def privatesuffix(self, domain: str) -> str | None: ...


# ``_PSL_UNSET`` keeps "never attempted" distinct from "attempted and
# unavailable": keep trying in the first, never again in the second.
_PSL_UNSET = None
_PSL: _PublicSuffixDataset | None = _PSL_UNSET


def _public_suffix_list() -> _PublicSuffixDataset:
    """Load the publicsuffixlist snapshot: a hard dependency, failure propagates.

    There is no silent absent state -- if the dataset cannot load, that error
    reaches the caller instead of degrading site grouping.
    """
    global _PSL
    if _PSL is not None:
        return _PSL
    _PSL = cast(_PublicSuffixDataset, import_module("publicsuffixlist").PublicSuffixList())
    return _PSL


def registrable_domain(host: str, *, psl: _PublicSuffixDataset | None = None) -> str:
    """The registrable site a host belongs to, or the host itself when no site.
    Hosts arrive canonical (punycode) from admission; a raw unicode host is
    encoded here so PSL rules keyed on punycode still match.
    """
    host = host.lower().rstrip(".")
    try:
        ascii_host = idna_host(host)
    except ValueError:
        ascii_host = host
    # An IP literal has no registrable site: the PSL '*' default would map it to
    # its last two octets, colliding unrelated hosts on one site key.
    if classify(ascii_host) is not AddressClass.NOT_AN_ADDRESS:
        return host
    if psl is None:
        psl = _public_suffix_list()
    registrable = psl.privatesuffix(ascii_host)
    return registrable if registrable else host


def is_public_suffix(host: str, *, psl: _PublicSuffixDataset | None = None) -> bool:
    """Whether ``host`` is itself a public suffix, with no registrable site under it.

    The cookie admission edge refuses a ``Domain=`` naming one of these:
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
    return psl.privatesuffix(host) is None


__all__ = ["is_public_suffix", "registrable_domain"]
