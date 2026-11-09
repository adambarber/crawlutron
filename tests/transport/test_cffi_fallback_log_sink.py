"""#660: the cffi fallback error sink logs redact_url()'s form, never the raw."""

import logging
import socket

import pytest

import crawlutron.transport.http_client_pool as http_mod
from crawlutron.transport.fetchers import cffi_fetch
from tests._policy_factories import canonical

_REACHABLE = [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
]


@pytest.fixture()
def _stopped_wire(monkeypatch):
    """A cffi session that raises on GET: the fetch fails, the fallback logs."""

    class _Probe:
        async def get(self, *args, **kwargs):
            raise RuntimeError("stop the hop")

    monkeypatch.setattr(http_mod, "get_cffi_session", lambda: _Probe())
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _REACHABLE)


async def test_fallback_log_record_carries_the_redacted_url(_stopped_wire, caplog):
    """A query secret in the fetched URL never reaches the log record."""
    with caplog.at_level(logging.WARNING, logger="crawlutron.transport.fetchers"):
        result = await cffi_fetch(
            canonical("http://example.com/feed?api_key=sk-live-123"),
            {},
            10,
            64 * 1024,
        )
    assert result is None
    records = [r for r in caplog.records if r.getMessage() == "cffi_fetch_fallback"]
    assert records, "the fallback error path did not log"
    logged = records[0].__dict__["url"]
    assert logged == "http://example.com/feed?api_key=[REDACTED]", logged
