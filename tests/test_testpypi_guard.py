"""The TestPyPI build-skip decision is pure, typed and fail-closed."""

from __future__ import annotations

import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


def test_every_present_version_skips_the_build() -> None:
    from scripts.testpypi_guard import should_skip

    assert should_skip({"crawlutron": "0.5.2", "crawlutron-admit": "0.1.0"}, lambda _n, _v: True)


def test_one_missing_distribution_forces_the_build() -> None:
    from scripts.testpypi_guard import should_skip

    versions = {"crawlutron": "0.5.2", "crawlutron-admit": "0.1.0"}
    assert not should_skip(versions, lambda name, _v: name != "crawlutron-admit")


def test_an_empty_roster_fails_closed() -> None:
    from scripts.testpypi_guard import should_skip

    assert not should_skip({}, lambda _n, _v: True)


def test_versions_are_derived_from_every_manifest(tmp_path: Path) -> None:
    from scripts.testpypi_guard import declared_versions

    (tmp_path / "pyproject.toml").write_text('[project]\nname="crawlutron"\nversion="0.5.2"\n')
    member = tmp_path / "packages" / "admit"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname="crawlutron-admit"\nversion="0.1.0"\n')
    assert declared_versions(tmp_path) == {"crawlutron": "0.5.2", "crawlutron-admit": "0.1.0"}


def test_plain_http_index_url_is_refused() -> None:
    from scripts.testpypi_guard import index_present

    with pytest.raises(ValueError, match="https"):
        index_present("crawlutron", "0.5.2", "http://127.0.0.1:1")


def test_a_missing_index_entry_is_a_miss_not_a_crash() -> None:
    from scripts.testpypi_guard import probe_index

    class Missing(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Missing)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port=server.server_port, timeout=5)
        assert probe_index(connection, "crawlutron-admit", "0.1.0") is False
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
