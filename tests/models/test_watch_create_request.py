"""Watch creation validates the webhook URL length at the model boundary."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from crawlutron.models.watch_create_request import WatchCreateRequest
from crawlutron.vocabulary.limits import MAX_WEBHOOK_URL_LENGTH
from tests._validation_helpers import _webhook_url_of_length


def test_watch_create_request_refuses_overlong_webhook_url():
    too_long = _webhook_url_of_length(MAX_WEBHOOK_URL_LENGTH + 1)
    with pytest.raises(ValidationError):
        WatchCreateRequest(
            url="https://example.com/",
            interval_seconds=60,
            webhook_url=too_long,
        )


def test_watch_create_request_accepts_boundary_webhook_url():
    boundary = _webhook_url_of_length(MAX_WEBHOOK_URL_LENGTH)
    request = WatchCreateRequest(
        url="https://example.com/",
        interval_seconds=60,
        webhook_url=boundary,
    )
    assert request.webhook_url == boundary
