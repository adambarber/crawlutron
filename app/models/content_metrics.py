"""The ``diagnostics["content_metrics"]`` block: what one body measured."""

from typing import TypedDict


class ContentMetrics(TypedDict, total=False):
    """The ``diagnostics["content_metrics"]`` block: what one body measured.

    Producers write the subset their body yielded, so every key is optional.
    """

    visible_text_length: int
    html_length: int
    link_count: int
    frame_count: int
    form_count: int
    has_frameset: bool
    text_to_html_ratio: int | float
