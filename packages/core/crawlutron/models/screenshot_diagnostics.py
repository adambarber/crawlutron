"""The ``diagnostics["screenshot"]`` block: one element-screenshot disclosure."""

from typing import TypedDict


class ScreenshotDiagnostics(TypedDict):
    """The ``diagnostics["screenshot"]`` block: one element-screenshot disclosure."""

    selector: str
    screenshot: str
