"""Adversarial fix (HIGH): decompression-bomb / oversized-body guards in the HTTP fetcher.

The fetcher must reject (413) responses that would expand to more than
``max_content_length`` bytes after decompression, and must cap raw bytes read
for chunked (no Content-Length) responses.
"""

import gzip
import http.server
import threading

from _testkit import _spawn_fetch, asyncio

from app.crawl.strategies.http import _default_http_fetcher


def _make_server():
    # ~5 KB on the wire, expands to 5 MB after gzip decompression.
    bomb_compressed = gzip.compress(b"A" * 5_000_000)
    ok_compressed = gzip.compress(b"hello world")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/bomb":
                body = bomb_compressed
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/ok":
                body = ok_compressed
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
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


async def test_gzip_bomb_rejected_with_413():
    srv = _make_server()
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        # Cap well below the bomb's expanded size.
        res = await _default_http_fetcher(f"http://127.0.0.1:{port}/bomb", {}, 10, 100_000)
        assert res.status_code == 413, res.status_code
        assert res.body == b""

        # A normal (small) gzipped response is decompressed correctly and the
        # content-encoding header is stripped so it isn't decoded twice.
        res2 = await _default_http_fetcher(f"http://127.0.0.1:{port}/ok", {}, 10, 1_000_000)
        assert res2.status_code == 200
        assert res2.body == b"hello world"
        assert "content-encoding" not in res2.headers
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=1.0)


async def test_chunked_response_without_content_length_is_capped():
    srv = _make_server()
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        # ~2 MB streamed with no Content-Length; cap at 100 KB -> rejected.
        res = await _default_http_fetcher(f"http://127.0.0.1:{port}/chunked_huge", {}, 10, 100_000)
        assert res.status_code == 413, res.status_code
        assert res.body == b""
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=1.0)


class TestFetchContentLengthLimit:
    def test_http_large_content(self):
        from app.crawl.strategies.http import _default_http_fetcher

        result = _spawn_fetch(lambda s: asyncio.run(_default_http_fetcher(s.base_url + "/", {}, 10, 1)))
        assert result.status_code == 413
