"""#137: a synthetic 413 classifies payload_too_large on every transport.

Red-first against the pre-fix tree: every test here failed with binary_no_text
or a decompression fault; the override row plus the constructor cleanups are the fix.
"""

from __future__ import annotations

from app.transport.cache import HttpCache
from app.transport.http import http_strategy_result
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._fetch_doubles import scripted_fetcher
from tests._fetcher_factories import make_fetch, run_http_strategy, strategy_request
from tests._policy_factories import http_strategy_policy

# The synthetic refusal both transports emit: 413, octet-stream, no body.
_413_OCTET = dict(status=413, ctype="application/octet-stream", body=b"")


async def test_httpx_413_octet_stream_classifies_payload_too_large():
    result = await run_http_strategy(
        url="http://example.com/",
        policy=http_strategy_policy(),
        http_fetcher=scripted_fetcher(**_413_OCTET),
    )
    assert result.classification == "payload_too_large"
    assert result.http_status == 413


async def test_cffi_413_octet_stream_classifies_payload_too_large():
    # The cffi refusal never echoes origin headers, so it shares the synthetic shape.
    async def cffi_413(url, *_):
        return make_fetch(url=url, final_url=url, **_413_OCTET)

    result = await run_http_strategy(
        url="http://example.com/",
        policy=http_strategy_policy(),
        http_fetcher=cffi_413,
    )
    assert result.classification == "payload_too_large"
    assert result.http_status == 413
    # No origin-header echo: the refusal carries only the synthetic content type.
    assert result.headers == {"content-type": "application/octet-stream"}


async def test_413_octet_stream_matches_the_real_strategy_path():
    result = await http_strategy_result(
        request=strategy_request(
            "http://example.com/",
            http_strategy_policy(),
            HTTP_DECLARED_CRAWLER,
        ),
        http_fetcher=scripted_fetcher(**_413_OCTET),
        cache=HttpCache(),
    )
    assert result.classification == "payload_too_large"


async def test_status_override_table_holds_its_413_row():
    result = await run_http_strategy(
        url="http://example.com/",
        policy=http_strategy_policy(),
        http_fetcher=scripted_fetcher(status=413, ctype="application/octet-stream", body=b""),
    )
    assert result.classification == "payload_too_large"
