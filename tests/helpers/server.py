"""Real, in-process HTTP test server (no mocks) serving the fixed routes the
crawler's actual-behavior tests need -- HTML with JSON-LD, robots/sitemap/feeds,
documents, and a conditional-request (304) route.
"""

from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

_HERE = os.path.dirname(os.path.abspath(__file__))
_FIXTURE_PDF = os.path.join(_HERE, "..", "fixtures", "sample_table.pdf")


JSONLD_HTML = """<!doctype html>
<html lang="en">
<head>
  <title>JSON-LD Example</title>
  <script type="application/ld+json">
  {"@context":"https://schema.org","@type":"Organization","name":"Example Org"}</script>
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

SHADOW_HTML = """<!doctype html>
<html lang="en"><head><title>Shadow Page</title></head>
<body>
  <h1>Shadow Host</h1>
  <div id="host"></div>
  <script>
    var host = document.getElementById('host');
    var sr = host.attachShadow({mode: 'open'});
    var a = document.createElement('a');
    a.href = '/shadow-link';
    a.textContent = 'Shadow Link';
    sr.appendChild(a);
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


def _route(status: int, content_type: str, body: bytes, extra: dict[str, str] | None = None) -> RouteEntry:
    """One route entry: status, headers (extra merged over the content type), body."""
    headers = {"content-type": content_type, **(extra or {})}
    return {"status": status, "headers": headers, "body": body}


RouteEntry = dict[str, Any]
"""One fixture route: status, headers, body -- the shape the handler serves."""


def _build_routes() -> dict[str, RouteEntry]:
    with open(_FIXTURE_PDF, "rb") as handle:
        pdf_bytes = handle.read()
    html = "text/html; charset=utf-8"
    return {
        "/": _route(200, html, JSONLD_HTML.encode("utf-8")),
        "/page2": _route(200, html, PAGE2_HTML.encode("utf-8")),
        "/set-cookie": _route(
            200,
            "text/plain; charset=utf-8",
            b"cookie set",
            extra={"set-cookie": "session_id=abc123; Path=/; HttpOnly; SameSite=Lax"},
        ),
        "/shadow": _route(200, html, SHADOW_HTML.encode("utf-8")),
        "/dynamic": _route(200, html, DYNAMIC_HTML.encode("utf-8")),
        "/lazy": _route(200, html, LAZY_HTML.encode("utf-8")),
        "/blog": _route(200, html, BLOG_HTML.encode("utf-8")),
        "/blocked": _route(200, html, BLOCKED_HTML.encode("utf-8")),
        "/robots.txt": _route(200, "text/plain", ROBOTS_TXT.encode("utf-8")),
        "/sitemap.xml": _route(200, "application/xml", SITEMAP_XML.encode("utf-8")),
        "/feed.xml": _route(200, "application/rss+xml", FEED_XML.encode("utf-8")),
        "/api/data": _route(200, "application/json", b'{"items":[1,2,3]}'),
        "/api/widgets": _route(200, "application/json", b'{"widgets":42}'),
        "/sample_table.pdf": _route(200, "application/pdf", pdf_bytes),
        "/sample.csv": _route(200, "text/csv", CSV_BODY.encode("utf-8")),
        "/sample.json": _route(200, "application/json", JSON_BODY.encode("utf-8")),
        "/with_etag": _route(200, html, b"<html><body>version one</body></html>", extra={"etag": "v1"}),
        "/broken.pdf": _route(200, "application/pdf", b"this is not actually a pdf"),
        "/with_lastmod": _route(
            200,
            html,
            b"<html><body>last modified page</body></html>",
            extra={"etag": "v2", "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"},
        ),
        "/ratelimit": _route(429, "text/plain", b"rate limited", extra={"retry-after": "30"}),
        "/legacy": _route(
            200,
            html,
            (
                b"<html><head><title>Legacy</title></head><body>"
                b'<frameset><frame src="/a"></frameset>'
                b'<form action="/submit" method="post"><input name="q" value="x"></form>'
                b'<a href="/page2">Next</a><a href="/page2">Next</a>'
                b"</body></html>"
            ),
        ),
        "/missing": _route(404, "text/plain", b"not found"),
    }


WEBHOOK_RECEIVED: list[bytes] = []


class _Handler(BaseHTTPRequestHandler):
    routes = _build_routes()

    def _respond(self, status: int, headers: dict[str, str], body: bytes) -> None:
        """Send one complete response, skipping the body for a HEAD.

        Every branch is the same four calls, and each copy is a place the HEAD guard or
        content-length goes missing; the 304 branch is deliberately NOT this -- no body, no length.
        """
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _handle(self) -> None:
        path = self.path.split("?", 1)[0]

        # Dynamic route: /check-cookie echoes the received Cookie header
        if path == "/check-cookie":
            cookie_header = self.headers.get("Cookie", "")
            self._respond(200, {"content-type": "text/plain; charset=utf-8"}, cookie_header.encode("utf-8"))
            return

        route = self.routes.get(path)
        if route is None:
            self._respond(404, {"content-type": "text/plain"}, b"not found")
            return

        # Conditional GET: honor If-None-Match with a 304.
        etag = route["headers"].get("etag")
        if etag and self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("etag", etag)
            self.end_headers()
            return

        self._respond(route["status"], route["headers"], route["body"])

    def do_GET(self) -> None:
        self._handle()

    def do_HEAD(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        WEBHOOK_RECEIVED.append(body)
        self._respond(200, {"content-type": "application/json"}, b"{}")

    def log_message(self, *args: Any) -> None:  # silence test noise
        return


class TestServer:
    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> TestServer:
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


# --- Special-purpose handlers for cache/revalidation fixtures (conftest.py) ---


class SilentHandler(BaseHTTPRequestHandler):
    """A request handler that answers with one body and says nothing in the log:
    ``send_body`` writes status, named headers, a content-length that matches the
    body (a mismatch was invisible), and ``log_message`` keeps one copy of the silence.
    """

    def send_body(self, status: int, body: bytes, **headers: str) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence
        return

    @property
    def counter(self) -> CountingHTTPServer:
        """The hit counter as the type it actually is: the one cast in this module,
        replacing three suppressions of the same error -- always a ``CountingHTTPServer``,
        so this narrows a known shape rather than hiding an unknown one.
        """
        return cast("CountingHTTPServer", self.server)


class CountingHTTPServer(ThreadingHTTPServer):
    """A threading server carrying the hit counter its handlers maintain: the base
    types declare no ``hits``, so handlers reached it by suppressing the same error;
    counter and accessor live here instead.
    """

    hits: int = 0


class CountingHandler(SilentHandler):
    """Serves one cacheable page and counts hits on ``server.hits``."""

    def do_GET(self):
        self.counter.hits += 1
        self.send_body(
            200,
            b"<html><body><h1>Hello</h1><p>cacheable content</p></body></html>",
            **{"content-type": "text/html; charset=utf-8"},
        )


class EtagHandler(SilentHandler):
    """Serves one page with a fixed etag and honors If-None-Match.

    Tests may temporarily mutate the ``etag`` / ``body`` class attributes
    to simulate an upstream content change.
    """

    etag = "v1"
    body = b"<html><body>etagged content</body></html>"

    def do_GET(self):
        if_none = self.headers.get("If-None-Match")
        counter = self.counter
        if if_none == self.etag:
            counter.hits += 1
            self.send_response(304)
            self.send_header("ETag", self.etag)
            self.end_headers()
            return
        counter.hits += 1
        self.send_body(200, self.body, **{"Content-Type": "text/html", "ETag": self.etag})


def spawn_threaded_server(handler_cls) -> CountingHTTPServer:
    """Start a daemon-served threading HTTP server on an ephemeral port.

    The caller owns teardown (``server.shutdown()`` + ``server.server_close()``);
    the ``counting_server`` / ``etag_server`` conftest fixtures do this.
    """
    server = CountingHTTPServer(("127.0.0.1", 0), handler_cls)
    server.hits = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
