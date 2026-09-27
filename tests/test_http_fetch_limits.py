"""Adversarial fix (HIGH): decompression-bomb / oversized-body guards in the HTTP fetcher.

The fetcher must reject (413) responses that would expand to more than
``max_content_length`` bytes after decompression, and must cap raw bytes read
for chunked (no Content-Length) responses.
"""

import asyncio
import gzip
import http.server
import threading
from contextlib import contextmanager

from _fetcher_factories import _spawn_fetch

from app.crawl.strategies.fetchers import _default_http_fetcher
from app.validation import HostPolicy


def _make_server():
    # ~5 KB on the wire, expands to 5 MB after gzip decompression.
    bomb_compressed = gzip.compress(b"A" * 5_000_000)
    ok_compressed = gzip.compress(b"hello world")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send_gzip(self, body: bytes) -> None:
            """One gzipped response. The bomb and the ordinary body differ only
            in what they carry -- a per-path copy of these five lines is a place
            for the two to stop being the same response."""
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/bomb":
                self._send_gzip(bomb_compressed)
            elif self.path == "/ok":
                self._send_gzip(ok_compressed)
            elif self.path == "/chunked_huge":
                # No Content-Length -> chunked; stream a large raw body.
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                for _ in range(2000):
                    try:
                        self.wfile.write(b"A" * 1000)
                    except BrokenPipeError:
                        # Client disconnected early (e.g., hit size cap)
                        break
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    return http.server.HTTPServer(("127.0.0.1", 0), Handler)


@contextmanager
def _running_server():
    """Serve the fixture site on a background thread; yield its port.

    The teardown is the part worth stating once: a leaked server or a joined
    thread outlives the test that forgot it, and the next test in the file
    inherits the port.
    """
    server = _make_server()
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


async def _fetch_with_cap(port: int, path: str, cap: int):
    """The real default fetcher against this fixture, with `cap` bytes allowed.

    The caps are the subject of every test in this file, so they are a parameter
    rather than part of a call site; ``policy`` is spelled here because a
    fixture this local has no SSRF stand-down and the loopback target is what
    the fetcher is being pointed at.
    """
    return await _default_http_fetcher(
        f"http://127.0.0.1:{port}{path}",
        {},
        10,
        cap,
        policy=HostPolicy.unrestricted(),
    )


async def test_gzip_bomb_rejected_with_413():
    with _running_server() as port:
        # Cap well below the bomb's expanded size.
        res = await _fetch_with_cap(port, "/bomb", 100_000)
        assert res.status_code == 413, res.status_code
        assert res.body == b""

        # A normal (small) gzipped response is decompressed correctly and the
        # content-encoding header is stripped so it isn't decoded twice.
        res2 = await _fetch_with_cap(port, "/ok", 1_000_000)
        assert res2.status_code == 200
        assert res2.body == b"hello world"
        assert "content-encoding" not in res2.headers


async def test_chunked_response_without_content_length_is_capped():
    with _running_server() as port:
        # ~2 MB streamed with no Content-Length; cap at 100 KB -> rejected.
        res = await _fetch_with_cap(port, "/chunked_huge", 100_000)
        assert res.status_code == 413, res.status_code
        assert res.body == b""


class TestFetchContentLengthLimit:
    def test_http_large_content(self):
        from app.crawl.strategies.fetchers import _default_http_fetcher

        result = _spawn_fetch(
            lambda s: asyncio.run(
                _default_http_fetcher(
                    s.base_url + "/",
                    {},
                    10,
                    1,
                    policy=HostPolicy.unrestricted(),
                )
            )
        )
        assert result.status_code == 413
