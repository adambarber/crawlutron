"""Live end-to-end feature verification for crawlutron.

Usage:
    python scripts/e2e_verify.py [base_url] [target_url]

Defaults: base_url=http://127.0.0.1:8765 target=https://example.com/
Exercises every public feature against a real target and exits non-zero on any
failure. Requires the service to be running (e.g. via Docker) with outbound
network access.
"""
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
TARGET = sys.argv[2] if len(sys.argv) > 2 else "https://example.com/"
results: list[tuple[str, bool]] = []


def check(name, ok, detail="") -> None:
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  | {str(detail)[:160]}" if detail and not ok else ""))


c = httpx.Client(timeout=180)

# Health / discovery surfaces
check("health", c.get(f"{BASE}/health").status_code == 200)
check("ready", c.get(f"{BASE}/ready").status_code == 200)
r = c.get(f"{BASE}/metrics")
check("metrics", r.status_code == 200 and "crawler_crawl" in r.text)
cap = c.get(f"{BASE}/v1/capabilities").json()
feats = cap.get("features", {})
check("capabilities truthful", feats.get("conditional_fetch") is True
      and feats.get("recursive_frontier") is True and feats.get("cloudflare_bypass") is False)
check("agent-hints", c.get(f"{BASE}/v1/agent-hints").status_code == 200)

# Single-page crawl of the live target
t0 = time.time()
r = c.post(f"{BASE}/v1/crawl", json={"url": TARGET})
ok = r.status_code == 200
body = r.json() if ok else {}
res = (body.get("results") or [{}])[0]
check("crawl sync 200", ok, r.text[:200])
elapsed = time.time() - t0
check(
    "rendered",
    res.get("status") == "success",
    f"{res.get('status')}/{res.get('classification')} in {elapsed:.1f}s",
)
check("markdown content", len(res.get("markdown") or "") > 200)
rid = body.get("request_id", "")
check("request_id issued", rid.startswith("crawl_"))

# Conditional revalidation when origin supports validators
validators = res.get("validators") or {}
etag = validators.get("etag")
if etag:
    r2 = c.post(f"{BASE}/v1/crawl", json={"url": TARGET, "validators": {TARGET: {"if_none_match": etag}}})
    res2 = (r2.json().get("results") or [{}])[0]
    check("conditional 304 not_modified",
          res2.get("http_status") == 304 and res2.get("classification") == "not_modified",
          f"http={res2.get('http_status')} cls={res2.get('classification')}")
else:
    print("INFO target sent no ETag; conditional check skipped")

# Recursive crawl with caps
r = c.post(f"{BASE}/v1/crawl", json={"url": TARGET, "policy": {
    "limits": {"max_depth": 1, "max_pages": 4, "max_discovered_links": 20}}})
b3 = r.json() if r.status_code == 200 else {}
check("recursive multi-page", len(b3.get("results") or []) >= 2,
      f"got {len(b3.get('results') or [])}")
diag = (b3.get("results") or [{}])[0].get("diagnostics", {})
check("discovery diagnostics", any(k in diag for k in ("robots", "js_endpoints", "sitemaps")))

# Result retrieval by request_id
check("result retrieval", c.get(f"{BASE}/v1/crawl/{rid}").status_code == 200)
miss = c.get(f"{BASE}/v1/crawl/crawl_doesnotexist999")
e = miss.json()
check("404 CrawlerError shape", miss.status_code == 404 and e.get("code") == "not_found")

# SSRF posture + validation shapes
r = c.post(f"{BASE}/v1/crawl", json={"url": "http://127.0.0.1:8765/health"})
check("SSRF internal rejected", r.status_code in (400, 422))
r = c.post(f"{BASE}/v1/crawl", json={"url": "not a url"})
e = r.json()
check("validation shape standardized", r.status_code == 422 and e.get("code") == "validation_error")
r = c.post(f"{BASE}/v1/crawl", json={"url": TARGET, "output_format": "bogus"})
check("output_format validated early", r.status_code == 422)

# Map endpoint
r = c.post(f"{BASE}/v1/map", json={"url": TARGET, "policy": {"limits": {"max_discovered_links": 20}}})
check("map endpoint", r.status_code == 200 and isinstance(r.json().get("links"), list))

passed = sum(1 for _, ok in results if ok)
print(f"===== {passed}/{len(results)} checks passed =====")
sys.exit(0 if passed == len(results) else 1)
