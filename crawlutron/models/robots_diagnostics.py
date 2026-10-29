"""The typed dictionary blocks carried inside a crawl's diagnostics report."""

from typing import NotRequired, TypedDict

from crawlutron.models.json_value import JsonValue


class RobotsDiagnostics(TypedDict):
    """The ``diagnostics["robots"]`` block: how one robots answer was reached.

    ``url``/``crawl_delay``/``error`` appear only on the branch that fetched;
    ``sitemaps`` is always present."""

    url: NotRequired[str | None]
    allowed: bool
    crawl_delay_seconds: NotRequired[float | None]
    sitemaps: list[str]
    reused_from_engine: NotRequired[bool]
    evaluation: str
    error: NotRequired[JsonValue]
