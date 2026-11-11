from fastapi import FastAPI
from starlette.requests import Request

from crawlutron.transport.body_decode import decode_body, quality_score
from crawlutron.transport.decompression import decompress_body
from crawlutron.transport.request_profile import cookie_header_for_url, headers_for_strategy
from crawlutron.vocabulary.strategies import HTTP_CFFI_IMPERSONATE


def minimal_request(path: str = "/") -> Request:
    """A starlette request whose app carries the one per-caller register."""
    from crawlutron.service.caller_concurrency import CallerConcurrencyTracker

    app = FastAPI()
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1),
            "scheme": "http",
            "app": app,
        }
    )


__all__ = [
    "HTTP_CFFI_IMPERSONATE",
    "cookie_header_for_url",
    "decode_body",
    "headers_for_strategy",
    "minimal_request",
    "quality_score",
    "decompress_body",
]
