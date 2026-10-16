from __future__ import annotations

from app.models.address_class import AddressClass, classify
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none

__all__ = ["filter_js_endpoints", "is_public_ip_value"]


def is_public_ip_value(value: str) -> bool:
    """Whether a host string is safe for public egress. Unlike the host policy's
    check this is asked of a *resolved* address, so a string that is not an
    address at all is not public here.
    """
    return classify(value) is AddressClass.PUBLIC


def filter_js_endpoints(
    endpoints: list[dict[str, str]],
    *,
    resolve_against: NormalizedUrl | None = None,
    policy: HostPolicy,
) -> list[dict[str, str]]:
    """The single egress filter for the JS-endpoint door (two builders once
    filtered with their own inline copies): an endpoint survives only if its URL
    passes ``NormalizedUrl.resolve``, carrying the filtered URL.
    """
    return [
        {**endpoint, "url": clean.raw}
        for endpoint in endpoints
        if (
            clean := (
                resolve_against.resolve(str(endpoint.get("url", "")))
                if resolve_against is not None
                else admit_or_none(str(endpoint.get("url", "")), policy=policy)
            )
        )
        is not None
    ]
