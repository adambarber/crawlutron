"""Test-only HTTP fixture that returns a unique success marker AND the received Host header."""

from __future__ import annotations

import http.server
import json
import socketserver
import uuid


class OriginHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that returns a unique marker and echoes the Host header."""

    # Class-level unique marker for this test run
    SUCCESS_MARKER = f"CRAWLUTRON_EGRESS_PROOF_{uuid.uuid4().hex[:16]}"

    def do_GET(self) -> None:
        host_header = self.headers.get("Host", "")
        response_data = {
            "success_marker": self.SUCCESS_MARKER,
            "host_header": host_header,
            "path": self.path,
        }
        body = json.dumps(response_data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Suppress default log messages
        pass


def run_server(port: int = 80) -> None:
    """Run the origin fixture server."""
    with socketserver.TCPServer(("0.0.0.0", port), OriginHandler) as httpd:
        print(f"Origin fixture listening on port {port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    run_server()
