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
from helpers.server import SilentHandler

from app.crawl.strategies.fetchers import _default_http_fetcher
from app.validation import HostPolicy


def _make_server():
    # ~5 KB on the wire, expands to 5 MB after gzip decompression.
    bomb_compressed = gzip.compress(b"A" * 5_000_000)
    ok_compressed = gzip.compress(b"hello world")

    class Handler(SilentHandler):
        def do_GET(self):
            if self.path in ("/bomb", "/ok"):
                # The bomb and the ordinary body differ only in what they carry,
                # so they go out through the one response shape: a per-path copy
                # of these headers is a place for the two to stop being the same
                # response, and the bomb's size would no longer be the only
                # difference between them.
                self.send_body(
                    200,
                    bomb_compressed if self.path == "/bomb" else ok_compressed,
                    **{"Content-Encoding": "gzip", "Content-Type": "text/plain"},
                )
            elif self.path == "/chunked_huge":
                # No Content-Length -> chunked; stream a large raw body. This one
                # cannot use send_body, which sets a content-length.
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


def assert_capped(res) -> None:
    """The fetch was refused for exceeding the cap, with no body.

    A 413 with a partial body is not a rejection the caller can act on -- the
    whole point of the cap is that the oversized content never lands. Asserting
    the status alone would pass for a response that streamed a megabyte and then
    gave up, which is the failure the cap exists to prevent.
    """
    assert res.status_code == 413, res.status_code
    assert res.body == b""


async def test_gzip_bomb_rejected_with_413():
    with _running_server() as port:
        # Cap well below the bomb's expanded size.
        assert_capped(await _fetch_with_cap(port, "/bomb", 100_000))

        # A normal (small) gzipped response is decompressed correctly and the
        # content-encoding header is stripped so it isn't decoded twice.
        res2 = await _fetch_with_cap(port, "/ok", 1_000_000)
        assert res2.status_code == 200
        assert res2.body == b"hello world"
        assert "content-encoding" not in res2.headers


async def test_chunked_response_without_content_length_is_capped():
    with _running_server() as port:
        # ~2 MB streamed with no Content-Length; cap at 100 KB -> rejected.
        assert_capped(await _fetch_with_cap(port, "/chunked_huge", 100_000))


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
