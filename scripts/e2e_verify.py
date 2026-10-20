"""Live end-to-end feature verification for crawlutron (import-safe, #285).

Usage: ``python scripts/e2e_verify.py [base_url] [target_url]`` — defaults
127.0.0.1:8765; requires the service running and outbound access.
"""

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


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    base = args[0] if len(args) > 0 else "http://127.0.0.1:8765"
    target = args[1] if len(args) > 1 else "https://example.com/"

    with httpx.Client(timeout=180) as c:
        _health(c, base)
        rid = _single_crawl(c, base, target)
        _retrieval(c, base, rid)
        _validation(c, base, target)

    passed = sum(1 for _, ok in results if ok)
    print(f"===== {passed}/{len(results)} checks passed =====")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
