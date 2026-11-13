"""A webhook URL whose query carries a secret-named parameter is refused at
admission: it would persist plaintext at rest (watches.webhook_url) and leave
at delivery time; webhook auth is policy.credentials, never URL params.
"""

import pytest
from safe_url.host_policy import HostPolicy

from crawlutron.models.webhook_target import WebhookTarget


def test_admit_refuses_secret_named_query_parameter():
    with pytest.raises(ValueError, match="token"):
        WebhookTarget.admit(
            "https://receiver.example/hook?token=secret123", host_policy=HostPolicy.unrestricted()
        )


def test_admit_still_admits_innocuous_query_parameters():
    target = WebhookTarget.admit(
        "https://receiver.example/hook?feed=rss&id=7", host_policy=HostPolicy.unrestricted()
    )
    assert target.delivery_url() == "https://receiver.example/hook?feed=rss&id=7"
