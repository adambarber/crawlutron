"""The browser_profile record: every key ``BROWSER_PROFILE_KEYS`` admits."""

from typing import Literal, TypedDict

from crawlutron.models.json_value import JsonValue


class BrowserProfileRecord(TypedDict, total=False):
    """Every key ``BROWSER_PROFILE_KEYS`` admits, at the types its readers narrow to.

    ``actions`` stays ``JsonValue``: the vocabulary closes it by isinstance on the wire.
    """

    viewport: Literal["desktop", "mobile"]
    resource_mode: Literal["full", "reduced"]
    storage_state: str
    actions: JsonValue
    include_html: bool
    include_screenshot: bool
    screenshot_selectors: list[str]
    auto_accept_cookie_consent: bool
    lazy_scroll_steps: int
    lazy_scroll_delay_seconds: int | float
    cookie_harvest_timeout_seconds: int | float
