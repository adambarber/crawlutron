"""A Request-shaped header carrier that is never opened (#544: zero noqa).
The stdlib cookiejar's send rules match a Request duck-type whose annotation
is over-narrow; the opener family Request belongs to is lint-audited (S310)."""

from __future__ import annotations


class HeaderCarrier:
    """Minimal Request stand-in for cookiejar.add_cookie_header: the jar's
    policies read ``full_url``/``get_full_url`` and write unredirected headers.
    This carrier never opens anything.
    """

    def __init__(self, url: str):
        self.full_url = url
        self.type = url.split(":", 1)[0] or "http"
        self.unredirected_hdrs: dict[str, str] = {}
        self.unverifiable = False
        self.host = ""
        self.origin_req_host = ""

    def get_full_url(self) -> str:
        return self.full_url

    def set_proxy(self, host: str, req_type: str) -> None:
        self.host = host
        self.type = req_type

    def has_proxy(self) -> bool:
        return False

    def get_method(self) -> str:
        return "GET"

    def add_header(self, key: str, val: str) -> None:
        self.unredirected_hdrs[key.capitalize()] = val

    def has_header(self, header_name: str) -> bool:
        return header_name.capitalize() in self.unredirected_hdrs

    def header_items(self) -> dict[str, str]:
        return dict(self.unredirected_hdrs)

    def remove_header(self, header_name: str) -> None:
        self.unredirected_hdrs.pop(header_name.capitalize(), None)

    def add_unredirected_header(self, key: str, value: str) -> None:
        self.unredirected_hdrs[key.capitalize()] = value

    def get_header(self, header_name: str, default: str | None = None) -> str | None:
        return self.unredirected_hdrs.get(header_name.capitalize(), default)


def cookie_header_request(raw_url: str) -> HeaderCarrier:
    """The header carrier for cookiejar matching (never opened -- see class)."""
    return HeaderCarrier(raw_url)
