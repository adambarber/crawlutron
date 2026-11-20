"""CI diagnostics: probe Firefox loopback connectivity to a local server.

Confirm which address family Firefox can reach on localhost.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from camoufox.async_api import AsyncCamoufox


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # silence
        pass


async def _probe(host: str, port: int) -> None:
    async with AsyncCamoufox(headless=True, humanize=True, i_know_what_im_doing=True) as browser:
        page = await browser.new_page()
        try:
            await page.goto(f"http://{host}:{port}/", timeout=15000)
            print(f"{host} OK")
        except Exception as exc:
            print(f"{host} FAIL: {str(exc).splitlines()[0]}")
        finally:
            await page.close()


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("getaddrinfo(localhost):")
    for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
        "localhost", port, type=socket.SOCK_STREAM
    ):
        print(f"  family={family} addr={sockaddr[0]}")

    asyncio.run(_probe("127.0.0.1", port))
    asyncio.run(_probe("localhost", port))
    asyncio.run(_probe("[::1]", port))

    server.shutdown()
    print("done")


if __name__ == "__main__":
    main()
