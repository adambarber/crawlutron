"""The ``diagnostics["screenshot"]`` block: one capped element-screenshot disclosure."""

import base64
from typing import Final, TypedDict

#: Raw bytes one screenshot may embed, capped at the capture chokepoint before base64. A covered page
#: renders a full-page PNG that can grow with the page height; this bounds the wire output either way.
MAX_SCREENSHOT_BYTES: Final[int] = 512 * 1024


class ScreenshotDiagnostics(TypedDict):
    """One element-screenshot disclosure: base64 under ``MAX_SCREENSHOT_BYTES``, else a refusal."""

    selector: str
    screenshot: str


def screenshot_payload(raw: bytes) -> str:
    """The base64 of a capture, or a bounded refusal diagnostic when the shot is oversize."""
    if len(raw) <= MAX_SCREENSHOT_BYTES:
        return base64.b64encode(raw).decode("ascii")
    return f"refused: screenshot {len(raw)} bytes exceeds {MAX_SCREENSHOT_BYTES} cap"


__all__ = ["MAX_SCREENSHOT_BYTES", "ScreenshotDiagnostics", "screenshot_payload"]
