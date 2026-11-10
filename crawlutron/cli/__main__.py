"""``crawl`` command line: thin HTTP client forwarding to the crawl service.

The CLI performs no SSRF, policy, or URL validation; it forwards URL and flags
to the service, which is the single source of truth.

Exit codes: 0 success, 1 partial (per --exit-on), 2 usage/401/422,
3 5xx/transport/timeout, 130 SIGINT.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

from .client import CrawlClient, CrawlClientError
from .docker import run_up
from .output import write_body

__all__ = ["main"]

DEFAULT_BASE_URL = "http://127.0.0.1:8765"
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_HTTP_TIMEOUT = 300.0
DEFAULT_DEEP_CRAWL_BFS_DEPTH = 2

EXIT_SUCCESS = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_SERVER_OR_TRANSPORT = 3
EXIT_SIGINT = 130

#: Cap on a single service body the CLI buffers into RAM. Service envelopes are
#: small JSON, so this is a backstop against a hostile or runaway payload, not a
#: size the CLI expects to approach. Kept out of crawlutron.safety because the
#: CLI is a pure transport facade that must not import safety/policy internals.
_CLIENT_BODY_CAP = 16 * 1024 * 1024

_OUTPUT_FORMATS = ("json", "jsonl", "ndjson", "markdown", "csv")
_DEEP_CRAWL_MODES = ("none", "bfs")
_EXIT_ON_MODES = ("none", "partial", "any")
_EXTRACTION_METHODS = ("full", "trafilatura", "document", "dom")


def _print_help() -> None:
    """Print compact usage to stderr."""
    sys.stderr.write(
        "usage: crawl <url> [options]\n"
        "       crawl up --image IMAGE --tag TAG\n"
        "       crawl service up --image IMAGE --tag TAG\n"
    )


def _crawl_args() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crawl")
    parser.add_argument("url", help="URL to crawl.")
    _add_output_and_recursion_args(parser)
    _add_limit_args(parser)
    _add_strategy_extraction_args(parser)
    _add_async_webhook_args(parser)
    _add_auth_trace_exit_args(parser)
    return parser


def _add_output_and_recursion_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o", "--output", choices=_OUTPUT_FORMATS, default="json", help="Output format (default json)."
    )
    parser.add_argument(
        "--deep-crawl",
        choices=_DEEP_CRAWL_MODES,
        default="none",
        help="Recursion mode: none = single page, bfs = recursive (default none).",
    )


def _add_limit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-pages", type=int, metavar="N", help="Total page cap (policy.limits.max_pages)."
    )
    parser.add_argument(
        "--max-depth", type=int, metavar="N", help="Recursion depth (policy.limits.max_depth)."
    )
    parser.add_argument(
        "--timeout",
        type=int,
        metavar="N",
        help="Per-strategy timeout seconds (policy.limits.timeout_seconds).",
    )
    parser.add_argument(
        "--max-retries", type=int, metavar="N", help="Retry count (policy.limits.max_retries)."
    )


def _add_strategy_extraction_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--strategy",
        action="append",
        metavar="STRATEGY",
        help="Fetch strategy; repeatable (policy.strategies).",
    )
    parser.add_argument(
        "--extraction", choices=_EXTRACTION_METHODS, help="Extraction method (policy.extraction)."
    )
    parser.add_argument(
        "--robots", action="store_true", default=None, help="Honor robots.txt (service default on)."
    )
    parser.add_argument(
        "--no-robots", action="store_false", dest="robots", default=None, help="Disable robots.txt."
    )


def _add_async_webhook_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--async", action="store_true", dest="async_mode", help="Accept 202 and return without waiting."
    )
    parser.add_argument("--wait", action="store_true", help="Submit async and poll until complete.")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        metavar="N",
        help="Seconds between status polls (default 1.0).",
    )
    parser.add_argument("--webhook", metavar="URL", help="Webhook delivery URL (async only).")


def _add_auth_trace_exit_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key", metavar="KEY", help="API key (env CRAWLUTRON_API_KEY).")
    parser.add_argument(
        "--base-url",
        metavar="URL",
        default=DEFAULT_BASE_URL,
        help="Service base URL (env CRAWLUTRON_BASE_URL, default %(default)s).",
    )
    parser.add_argument("--trace", action="store_true", help="Also print the crawl trace.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    parser.add_argument(
        "--exit-on",
        choices=_EXIT_ON_MODES,
        default="none",
        help="Exit 1 when: none=never, partial=success false, any=any result failed.",
    )


def _parse_args(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace | int:
    try:
        return parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None or code == 0:
            return EXIT_SUCCESS
        return EXIT_USAGE


def _build_payload(args: argparse.Namespace) -> dict[str, object]:
    """Map CLI flags 1:1 to wire fields; omit anything not explicitly given."""
    payload: dict[str, object] = {"url": args.url}
    policy: dict[str, object] = {}
    limits: dict[str, object] = {}

    if args.strategy:
        policy["strategies"] = list(args.strategy)
    if args.extraction:
        policy["extraction"] = args.extraction
    if args.timeout is not None:
        limits["timeout_seconds"] = args.timeout
    if args.max_retries is not None:
        limits["max_retries"] = args.max_retries
    if args.max_pages is not None:
        limits["max_pages"] = args.max_pages
    if args.max_depth is not None:
        limits["max_depth"] = args.max_depth
    if args.robots is not None:
        limits["respect_robots"] = args.robots
    if args.deep_crawl == "bfs" and args.max_depth is None:
        limits["max_depth"] = DEFAULT_DEEP_CRAWL_BFS_DEPTH
    if args.deep_crawl == "none":
        limits["max_depth"] = 0

    if limits:
        policy["limits"] = limits
    if policy or limits:
        payload["policy"] = policy
    if args.async_mode:
        payload["async_mode"] = True
    if args.webhook:
        payload["webhook_url"] = args.webhook
    if args.output:
        payload["output_format"] = args.output
    return payload


def _read_body(response: httpx.Response, cap: int = _CLIENT_BODY_CAP) -> bytes:
    """Read one service body, capped, streaming through ``iter_bytes``.

    httpx ``Response.read()`` / ``.content`` pulls the whole body into RAM
    unbounded; this streams it through ``iter_bytes`` and raises as soon as the
    cap is hit. The receiver is read via ``iter_bytes`` (not ``.read()``), so the
    body-read invariant that bans bare ``response.read()`` stays satisfied while
    the CLI never buffers a hostile body whole.
    """
    raw = bytearray()
    for chunk in response.iter_bytes():
        raw.extend(chunk)
        if len(raw) > cap:
            raise CrawlClientError(f"service body exceeds {cap}-byte cap")
    return bytes(raw)


def _handle_status(code: int, response: httpx.Response) -> int:
    """Convert a non-200 service response to an exit code, surfacing the envelope."""
    text = _read_body(response).decode("utf-8", errors="replace")
    if 400 <= code < 500:
        sys.stderr.write(text + "\n")
        return EXIT_USAGE
    sys.stderr.write(text + "\n")
    return EXIT_SERVER_OR_TRANSPORT


def _exit_code_from_body(body: bytes, exit_on: str) -> int:
    """Decide whether a completed crawl is partial per --exit-on semantics."""
    if exit_on == "none":
        return EXIT_SUCCESS
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return EXIT_SUCCESS
    if exit_on == "partial":
        return EXIT_SUCCESS if bool(data.get("success", True)) else EXIT_PARTIAL
    # exit_on == "any": any individual result carrying an error makes it partial.
    for result in data.get("results") or []:
        if isinstance(result, dict) and result.get("error"):
            return EXIT_PARTIAL
    return EXIT_SUCCESS


def _render_200(response: httpx.Response, args: argparse.Namespace) -> tuple[int, bytes] | None:
    """Render a 200 service body (write it, compute its exit code) or None."""
    if response.status_code != 200:
        return None
    body = _read_body(response)
    write_body(body, output=args.output, pretty=args.pretty)
    return _exit_code_from_body(body, args.exit_on), body


def _wait_and_print(client: CrawlClient, request_id: str, args: argparse.Namespace) -> int:
    """Poll GET /v1/crawl/{request_id} until terminal, then render and exit."""
    while True:
        response = client.result(request_id)
        rendered = _render_200(response, args)
        if rendered is not None:
            return rendered[0]
        if response.status_code == 202:
            time.sleep(args.poll_interval)
            continue
        return _handle_status(response.status_code, response)


def _handle_sync(client: CrawlClient, response: httpx.Response, args: argparse.Namespace) -> int:
    rendered = _render_200(response, args)
    if rendered is None:
        return _handle_status(response.status_code, response)
    exit_code, body = rendered
    if args.trace:
        try:
            data = json.loads(body)
            request_id = str(data.get("request_id", ""))
        except (json.JSONDecodeError, ValueError):
            request_id = ""
        if request_id:
            trace_response = client.trace(request_id)
            if trace_response.status_code == 200:
                sys.stdout.write("\n")
                write_body(_read_body(trace_response))
            else:
                return _handle_status(trace_response.status_code, trace_response)
    return exit_code


def run_crawl(
    argv: list[str],
    client: CrawlClient | None = None,
) -> int:
    """Run the crawl command; ``client`` is injected for tests (MockTransport)."""
    parser = _crawl_args()
    parsed = _parse_args(parser, argv)
    if isinstance(parsed, int):
        return parsed
    args = parsed

    api_key = args.key or os.environ.get("CRAWLUTRON_API_KEY")
    base_url = os.environ.get("CRAWLUTRON_BASE_URL") or args.base_url
    payload = _build_payload(args)

    own_client = False
    if client is None:
        client = CrawlClient(base_url=base_url, api_key=api_key, timeout=DEFAULT_HTTP_TIMEOUT)
        own_client = True
    try:
        async_door = args.wait or args.async_mode
        if async_door:
            payload = dict(payload)
            payload["async_mode"] = True
        response = client.crawl(payload)
        if async_door and response.status_code == 202:
            body = _read_body(response)
            write_body(body)
            if not args.wait:
                return EXIT_SUCCESS
            try:
                envelope = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return EXIT_SUCCESS
            request_id = str(envelope.get("request_id", ""))
            if not request_id:
                return EXIT_SUCCESS
            return _wait_and_print(client, request_id, args)
        return _handle_sync(client, response, args)
    except CrawlClientError as exc:
        sys.stderr.write(str(exc) + "\n")
        return EXIT_SERVER_OR_TRANSPORT
    except KeyboardInterrupt:
        return EXIT_SIGINT
    finally:
        if own_client:
            client.close()


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse argv, dispatch to crawl or docker lifecycle."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        _print_help()
        return EXIT_USAGE
    head, rest = argv[0], argv[1:]
    if head == "service":
        if not rest or rest[0] != "up":
            _print_help()
            return EXIT_USAGE
        return run_up(rest[1:])
    if head == "up":
        return run_up(rest)
    return run_crawl(argv)


if __name__ == "__main__":
    sys.exit(main())
