from __future__ import annotations

from app.addresses import AddressClass, classify
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, admit_or_none


def is_public_ip_value(value: str) -> bool:
    """Return whether a host string is safe for public egress.

    Unlike ``app.models.host_policy._host_is_public`` this is asked of a
    *resolved* address, so a string that is not an address at all is not public
    here.
    """
    return classify(value) is AddressClass.PUBLIC


def filter_js_endpoints(
    endpoints: list[dict[str, str]],
    *,
    resolve_against: NormalizedUrl | None = None,
    policy: HostPolicy,
) -> list[dict[str, str]]:
    """Egress filter for the JS-endpoint discovery door.

    ``extract_js_endpoints`` scrapes URLs out of attacker-controlled HTML/JS
    and hands back ``{"url": ...}`` dicts. Two builders (HTTP and browser)
    each filtered that list with the same inline comprehension -- the shape
    that drifts the moment a third consumer forgets the copy. This is the
    single filter for the door: an endpoint survives only if its URL passes
    :meth:`NormalizedUrl.resolve` (resolve relatives first, then accept only
    http(s)), and the surviving endpoint carries the filtered URL so callers
    never see the raw scraped string.
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
