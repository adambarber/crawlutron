"""One XHR the browser observed, in ``diagnostics["intercepted_xhr"]``."""

from typing import NotRequired, TypedDict

from app.models.json_value import JsonValue


class InterceptedXhrRecord(TypedDict, total=False):
    """One XHR the browser observed; the worker's disclosure fields are open."""

    url: NotRequired[str]
    method: NotRequired[str]
    content_type: NotRequired[str]
    status: NotRequired[int]
    payload: NotRequired[JsonValue]
    url_truncated: NotRequired[bool]
    payload_truncated: NotRequired[bool]
