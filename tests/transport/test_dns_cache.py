"""DNS resolution for the pin path: it resolves fresh each call so a rebind
inside the TTL cannot bypass SSRF.
"""

import socket
import uuid
from unittest.mock import patch

from crawlutron.runtime import process_state
from crawlutron.transport import pinned_dns as http_mod
from tests._policy_factories import canonical


def _counting_resolver(ip: str = "8.8.8.8"):
    """Return (call_counter, fake_getaddrinfo) that resolves every host to ``ip``."""
    calls = {"n": 0}

    def fake_getaddrinfo(_host, *a, **k):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    return calls, fake_getaddrinfo


def test_resolve_pin_host_resolves_fresh_each_call(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    host = f"b-{uuid.uuid4().hex}.example.com"
    calls, fake = _counting_resolver()
    process_state.reset_all_process_state()
    with patch("socket.getaddrinfo", side_effect=fake):
        h1, ip1 = http_mod.resolve_and_pin_host(canonical(f"http://{host}/p"))
        h2, ip2 = http_mod.resolve_and_pin_host(canonical(f"http://{host}/q"))
    # pin path is intentionally NOT cached (security): resolves each call
    assert calls["n"] == 2, calls
    assert ip1 == ip2 == "8.8.8.8"
