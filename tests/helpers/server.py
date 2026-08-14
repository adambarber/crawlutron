"""Real, in-process HTTP test server (no mocks).

Serves a fixed set of routes used by the crawler's actual-behavior tests:
HTML pages with JSON-LD, pagination, inline JS fetch (XHR), lazy-load
scroll triggers, robots/sitemap/feeds, documents (PDF/CSV/JSON), and a
route that honors ``If-None-Match`` conditional requests (304).
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURE_PDF = os.path.join(_HERE, "..", "fixtures", "sample_table.pdf")


JSONLD_HTML = """<!doctype html>
<html lang="en">
<head>
  <title>JSON-LD Example</title>
  <script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Example Org"}</script>
</head>
<body>
  <h1>Example Org</h1>
  <p>Some real content for extraction.</p>
  <a rel="next" href="/page2">Next</a>
</body>
</html>
"""

PAGE2_HTML = """<!doctype html>
<html><head><title>Page 2</title></head>
<body><h1>Page Two</h1><p>Second page content.</p></body></html>
"""

DYNAMIC_HTML = """<!doctype html>
<html><head><title>Dynamic Page</title></head>
<body>
  <h1>Dynamic Page</h1>
  <p>Loaded with XHR data below:</p>
  <script>
    fetch('/api/data').then(function(r){return r.json();}).then(function(d){
      var el = document.createElement('div');
      el.id = 'data';
      el.textContent = JSON.stringify(d);
      document.body.appendChild(el);
    });
  </script>
  <script>
    if (window.axios) { axios.get('/api/widgets'); }
  </script>
</body></html>
"""

LAZY_HTML = """<!doctype html>
<html><head><title>Lazy Page</title></head>
<body>
  <p>Visible content</p>
  <div id="lazy" style="display:none">Lazy Content Revealed</div>
  <script>
    window.addEventListener('scroll', function(){
      document.getElementById('lazy').style.display = 'block';
    });
  </script>
</body></html>
"""

BLOG_HTML = """<!doctype html>
<html><head>
  <title>Blog</title>
  <link rel="alternate" type="application/rss+xml" title="RSS" href="/feed.xml">
</head><body><h1>Blog</h1><p>Posts here.</p></body></html>
"""

BLOCKED_HTML = """<!doctype html>
<html><head><title>Denied</title></head>
<body><h1>Access Denied</h1><p>You do not have permission to view this page.</p></body></html>
"""

ROBOTS_TXT = "User-agent: *\nAllow: /\nSitemap: /sitemap.xml\n"
SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>/page1</loc></url>
  <url><loc>/page2</loc></url>
</urlset>
"""
FEED_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog RSS</title>
  <link>https://example.com/feed.xml</link>
  <item><title>Post 1</title><link>https://example.com/post1</link></item>
</channel></rss>
"""
CSV_BODY = "name,age\nAlice,30\nBob,25\n"
JSON_BODY = '{"a":1,"b":2}'


def _build_routes() -> dict[str, dict[str, Any]]:
    with open(_FIXTURE_PDF, "rb") as handle:
        pdf_bytes = handle.read()
    return {
        "/": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": JSONLD_HTML.encode("utf-8"),
        },
        "/page2": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": PAGE2_HTML.encode("utf-8"),
        },
        "/dynamic": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": DYNAMIC_HTML.encode("utf-8"),
        },
        "/lazy": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": LAZY_HTML.encode("utf-8"),
        },
        "/blog": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": BLOG_HTML.encode("utf-8"),
        },
        "/blocked": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": BLOCKED_HTML.encode("utf-8"),
        },
        "/robots.txt": {
            "status": 200,
            "headers": {"content-type": "text/plain"},
            "body": ROBOTS_TXT.encode("utf-8"),
        },
        "/sitemap.xml": {
            "status": 200,
            "headers": {"content-type": "application/xml"},
            "body": SITEMAP_XML.encode("utf-8"),
        },
        "/feed.xml": {
            "status": 200,
            "headers": {"content-type": "application/rss+xml"},
            "body": FEED_XML.encode("utf-8"),
        },
        "/api/data": {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": b'{"items":[1,2,3]}',
        },
        "/api/widgets": {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": b'{"widgets":42}',
        },
        "/sample_table.pdf": {
            "status": 200,
            "headers": {"content-type": "application/pdf"},
            "body": pdf_bytes,
        },
        "/sample.csv": {
            "status": 200,
            "headers": {"content-type": "text/csv"},
            "body": CSV_BODY.encode("utf-8"),
        },
        "/sample.json": {
            "status": 200,
            "headers": {"content-type": "application/json"},
            "body": JSON_BODY.encode("utf-8"),
        },
        "/with_etag": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8", "etag": "v1"},
            "body": b"<html><body>version one</body></html>",
        },
        "/broken.pdf": {
            "status": 200,
            "headers": {"content-type": "application/pdf"},
            "body": b"this is not actually a pdf",
        },
        "/with_lastmod": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8", "etag": "v2", "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
            "body": b"<html><body>last modified page</body></html>",
        },
        "/ratelimit": {
            "status": 429,
            "headers": {"content-type": "text/plain", "retry-after": "30"},
            "body": b"rate limited",
        },
        "/legacy": {
            "status": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "body": (
                b'<html><head><title>Legacy</title></head><body>'
                b'<frameset><frame src="/a"></frameset>'
                b'<form action="/submit" method="post"><input name="q" value="x"></form>'
                b'<a href="/page2">Next</a><a href="/page2">Next</a>'
                b'</body></html>'
            ),
        },
        "/missing": {"status": 404, "headers": {"content-type": "text/plain"}, "body": b"not found"},
    }


WEBHOOK_RECEIVED: list[bytes] = []


class _Handler(BaseHTTPRequestHandler):
    routes = _build_routes()

    def _handle(self) -> None:
        route = self.routes.get(self.path.split("?", 1)[0])
        if route is None:
            self.send_response(404)
            self.send_header("content-type", "text/plain")
            self.send_header("content-length", "9")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(b"not found")
            return

        # Conditional GET: honor If-None-Match with a 304.
        etag = route["headers"].get("etag")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("etag", etag)
            self.end_headers()
            return

        body = route["body"]
        self.send_response(route["status"])
        for key, value in route["headers"].items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        WEBHOOK_RECEIVED.append(body)
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: Any) -> None:  # silence test noise
        return


class TestServer:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "TestServer":
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def spawn_server() -> TestServer:
    return TestServer().start()
