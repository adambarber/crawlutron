"""#342: ``post_webhook`` must not materialise the payload 3x before the size check.

The old shape built ``dict(payload)`` + the full JSON str + the full bytes before
``len()``; the serialiser now bails as soon as the running byte count crosses the cap.
"""

from __future__ import annotations

import asyncio
import json
import tracemalloc
import unittest.mock as mock

import pytest

from crawlutron.api import webhooks
from crawlutron.models.host_policy import HostPolicy


def _payload(n_results: int) -> dict[str, object]:
    return {
        "request_id": "crawl_probe",
        "results": [
            {"url": f"http://example.com/{i}", "text": "x" * 200, "markdown": None} for i in range(n_results)
        ],
    }


def _probe_peak(payload) -> tuple[int, int | None]:
    """Peak traced bytes over one ``post_webhook`` call, plus the delivered size."""
    with mock.patch.object(webhooks, "deliver_pinned_webhook", mock.AsyncMock()) as deliver:
        tracemalloc.start()
        asyncio.run(
            webhooks.post_webhook(
                "https://webhook.example.com/cb",
                payload,
                host_policy=HostPolicy.unrestricted(),
            )
        )
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    delivered = len(deliver.await_args.args[1]) if deliver.await_count else None
    return peak, delivered


@pytest.mark.parametrize(
    ("n_results", "cap", "ratio"),
    [(6000, 10_485_760, 1.5), (200_000, 10_485_760, 1.1)],
    ids=["near-cap", "oversized"],
)
def test_payload_peak_stays_bounded(monkeypatch, n_results, cap, ratio):
    """A near-cap payload peaks under 1.5x delivered; an oversized one never materialises."""
    monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_SIZE", str(cap))
    payload = _payload(n_results)
    peak, delivered = _probe_peak(payload)
    if delivered is not None:
        assert delivered == len(json.dumps(payload))
    else:
        assert n_results != 6000, "the near-cap payload must still be delivered"
    assert peak < ratio * (delivered or cap), f"peak {peak} vs bound {ratio * (delivered or cap)}"
