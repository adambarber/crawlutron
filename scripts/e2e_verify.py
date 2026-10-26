"""Live end-to-end feature verification for crawlutron (import-safe, #285).

Usage: ``python scripts/e2e_verify.py [base_url] [target_url]`` — defaults
127.0.0.1:8765; requires the service running and outbound access.
"""

import os
import sys
import time

import httpx

results: list[tuple[str, bool]] = []


def check(name, ok, detail="") -> None:
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  | {str(detail)[:160]}" if detail and not ok else ""))


def _health(c: httpx.Client, base: str) -> None:
    check("health", c.get(f"{base}/health").status_code == 200)
    check("ready", c.get(f"{base}/ready").status_code == 200)
    r = c.get(f"{base}/metrics")
    check("metrics", r.status_code == 200 and "crawler_crawl" in r.text)
    cap = c.get(f"{base}/v1/capabilities").json()
    feats = cap.get("features", {})
    check(
        "capabilities truthful",
        feats.get("conditional_fetch") is True
        and feats.get("recursive_frontier") is True
        and feats.get("captcha_solving") is False,
    )
    check("agent-hints", c.get(f"{base}/v1/agent-hints").status_code == 200)


def _single_crawl(c: httpx.Client, base: str, target: str) -> str:
    t0 = time.time()
    r = c.post(f"{base}/v1/crawl", json={"url": target})
    ok = r.status_code == 200
    body = r.json() if ok else {}
    res = (body.get("results") or [{}])[0]
    check("crawl sync 200", ok, r.text[:200])
    check(
        "rendered",
        res.get("status") == "success",
        f"{res.get('status')}/{res.get('classification')} in {time.time() - t0:.1f}s",
    )
    check("markdown content", len(res.get("markdown") or "") > 200)
    rid = body.get("request_id", "")
    check("request_id issued", rid.startswith("crawl_"))

    validators = res.get("validators") or {}
    etag = validators.get("etag")
    if etag:
        r2 = c.post(
            f"{base}/v1/crawl",
            json={"url": target, "validators": {target: {"if_none_match": etag}}},
        )
        res2 = (r2.json().get("results") or [{}])[0]
        check(
            "conditional 304 not_modified",
            res2.get("http_status") == 304 and res2.get("classification") == "not_modified",
            f"http={res2.get('http_status')} cls={res2.get('classification')}",
        )
    else:
        print("INFO target sent no ETag; conditional check skipped")

    r = c.post(
        f"{base}/v1/crawl",
        json={
            "url": target,
            "policy": {"limits": {"max_depth": 1, "max_pages": 4, "max_discovered_links": 20}},
        },
    )
    b3 = r.json() if r.status_code == 200 else {}
    check("recursive multi-page", len(b3.get("results") or []) >= 2, f"got {len(b3.get('results') or [])}")
    diag = (b3.get("results") or [{}])[0].get("diagnostics", {})
    check("discovery diagnostics", any(k in diag for k in ("robots", "js_endpoints", "sitemaps")))
    return rid


def _retrieval(c: httpx.Client, base: str, rid: str) -> None:
    check("result retrieval", c.get(f"{base}/v1/crawl/{rid}").status_code == 200)
    miss = c.get(f"{base}/v1/crawl/crawl_doesnotexist999")
    e = miss.json()
    check("404 CrawlerError shape", miss.status_code == 404 and e.get("code") == "not_found")


def _validation(c: httpx.Client, base: str, target: str) -> None:
    r = c.post(f"{base}/v1/crawl", json={"url": "http://127.0.0.1:8765/health"})
    check("SSRF internal rejected", r.status_code in (400, 422))
    r = c.post(f"{base}/v1/crawl", json={"url": "not a url"})
    e = r.json()
    check("validation shape standardized", r.status_code == 422 and e.get("code") == "validation_error")
    r = c.post(f"{base}/v1/crawl", json={"url": target, "output_format": "bogus"})
    check("output_format validated early", r.status_code == 422)
    r = c.post(f"{base}/v1/map", json={"url": target, "policy": {"limits": {"max_discovered_links": 20}}})
    check("map endpoint", r.status_code == 200 and isinstance(r.json().get("links"), list))


def _well_known(c: httpx.Client, base: str) -> None:
    """Discovery surface the agent contract names first (#291)."""
    wk = c.get(f"{base}/.well-known/crawler-service")
    body = wk.json() if wk.status_code == 200 else {}
    links = body.get("links", {})
    check("well-known crawler-service", wk.status_code == 200, wk.text[:160])
    check(
        "well-known advertises discovery links",
        "capabilities" in links
        and "result_retrieval" in links
        and isinstance(body.get("recommended_endpoints"), list),
        wk.text[:160],
    )
    check("well-known service identity", bool(body.get("service")) and bool(body.get("version")))


def _auth(c: httpx.Client, base: str, target: str) -> None:
    """Keyed-caller surface: issuance, 401, 429, and the isolation edge (#291)."""
    admin = os.environ.get("CRAWLUTRON_KEY_ADMIN_TOKEN", "")
    if not admin:
        print("INFO CRAWLUTRON_KEY_ADMIN_TOKEN unset; keyed/401/429 checks skipped")
        return
    check("keys missing key -> 401", _refused_401(c.get(f"{base}/v1/keys")))
    check("keys wrong key -> 401", _refused_401(c.get(f"{base}/v1/keys", headers={"X-API-Key": "bad-token"})))
    issued = c.post(
        f"{base}/v1/keys",
        headers={"X-API-Key": admin},
        json={"label": "e2e-verify", "max_pages": 1},
    )
    if issued.status_code != 201:
        check("key issued 201", False, issued.text[:160])
        return
    secret = issued.json()["secret"]
    check("key issued 201", True)
    kr = c.post(f"{base}/v1/crawl", headers={"X-API-Key": secret}, json={"url": target})
    check("keyed crawl works", kr.status_code == 200, kr.text[:120])
    ridb = kr.json().get("request_id", "")

    other = c.post(f"{base}/v1/keys", headers={"X-API-Key": admin}, json={"label": "e2e-other"})
    if other.status_code == 201:
        _isolation(c, base, ridb, secret, other.json()["secret"])
    else:
        check("cross-key read isolation", False, other.text[:120])

    over = c.post(
        f"{base}/v1/crawl",
        headers={"X-API-Key": secret},
        json={"url": target, "policy": {"limits": {"max_pages": 50}}},
    )
    check(
        "over-budget keyed crawl -> 429",
        over.status_code == 429 and over.json().get("code") == "quota_exceeded",
        over.text[:120],
    )


def _isolation(c: httpx.Client, base: str, rid: str, owner: str, other: str) -> None:
    """Caller A's job is 404 to caller B, and readable to A (#291)."""
    if not rid:
        check("cross-key read isolation", False, "keyed crawl returned no request_id")
        return
    mine = c.get(f"{base}/v1/crawl/{rid}", headers={"X-API-Key": owner})
    theirs = c.get(f"{base}/v1/crawl/{rid}", headers={"X-API-Key": other})
    check(
        "cross-key read isolation (B can't read A)",
        theirs.status_code == 404 and mine.status_code == 200,
        f"mine={mine.status_code} theirs={theirs.status_code}",
    )


def _refused_401(response: httpx.Response) -> bool:
    """A 401 with the invalid_api_key envelope (live service unwraps it)."""
    return response.status_code == 401 and response.json().get("code") == "invalid_api_key"


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args in (["-h"], ["--help"]):
        print(__doc__.strip())
        return 0
    base = args[0] if len(args) > 0 else "http://127.0.0.1:8765"
    target = args[1] if len(args) > 1 else "https://example.com/"

    with httpx.Client(timeout=180) as c:
        _health(c, base)
        _well_known(c, base)
        rid = _single_crawl(c, base, target)
        _retrieval(c, base, rid)
        _validation(c, base, target)
        _auth(c, base, target)

    passed = sum(1 for _, ok in results if ok)
    print(f"===== {passed}/{len(results)} checks passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
