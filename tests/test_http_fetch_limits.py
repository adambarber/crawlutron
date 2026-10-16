"""Adversarial fix (HIGH): the fetcher must reject (413) any response that would
expand past ``max_content_length`` after decompression, and must cap raw bytes
read for chunked / no-Content-Length responses.
"""

import asyncio
import gzip
import http.server
import threading
from contextlib import contextmanager

from app.transport.fetchers import default_http_fetcher
from tests._fetcher_factories import _spawn_fetch
from tests._policy_factories import canonical
from tests.helpers.server import SilentHandler


def _make_server():
    # ~5 KB on the wire, expands to 5 MB after gzip decompression.
    bomb_compressed = gzip.compress(b"A" * 5_000_000)
    ok_compressed = gzip.compress(b"hello world")

    class Handler(SilentHandler):
        def do_GET(self):
            if self.path in ("/bomb", "/ok"):
                self.send_body(
                    200,
                    bomb_compressed if self.path == "/bomb" else ok_compressed,
                    **{"Content-Encoding": "gzip", "Content-Type": "text/plain"},
                )
            elif self.path == "/chunked_huge":
                # No Content-Length -> chunked; stream a large raw body. This one cannot use send_body, which
                # sets a content-length.
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
    """Serve the fixture on a background thread; yield its port. The teardown is the
    worth-stating half: a leaked server or joined thread outlives the test that forgot
    it and the next test inherits the port.
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
    """The real default fetcher against this fixture, with `cap` bytes allowed. The
    caps are the subject of every test, so they are a parameter; ``policy`` is spelled
    because this local fixture has no SSRF stand-down and the loopback is the target.
    """
    return await default_http_fetcher(
        canonical(f"http://127.0.0.1:{port}{path}"),
        {},
        10,
        cap,
    )


def assert_capped(res) -> None:
    """Refused for exceeding the cap, with no body: a 413 with a partial body is not a
    rejection the caller can act on, and asserting only the status would pass a stream
    that gave up after a megabyte -- the failure the cap exists to prevent.
    """
    assert res.status_code == 413, res.status_code
    assert res.body == b""


async def test_gzip_bomb_rejected_with_413():
    with _running_server() as port:
        # Cap well below the bomb's expanded size.
        assert_capped(await _fetch_with_cap(port, "/bomb", 100_000))

        # A normal (small) gzipped response is decompressed correctly and the content-encoding header is
        # stripped so it isn't decoded twice.
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
        from app.transport.fetchers import default_http_fetcher

        result = _spawn_fetch(
            lambda s: asyncio.run(
                default_http_fetcher(
                    canonical(s.base_url + "/"),
                    {},
                    10,
                    1,
                )
            )
        )
        assert result.status_code == 413
