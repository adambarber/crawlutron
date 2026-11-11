# Crawlutron — Adversarial Review & Open-Source Readiness Report

**Scope:** full `crawlutron/` source tree (262 modules, ~26,200 LOC), plus README / CONTRIBUTING / SECURITY.md / CHANGELOG / prior-review docs.
**Method:** ten independent adversarial passes over module partitions (admission, API, transport, orchestration, storage, runtime/safety, models, extraction, search), followed by direct verification of every P1 claim against source. Line anchors are module-relative (`crawlutron/models/normalized_url.py:NN`) as of this tree.
**Stance:** adversarial. Every claim below was checked in code, not inferred from names or docstrings. Items that looked exploitable but turned out to be guarded are listed as "checked clean" so you know they were covered.

---

## 0. Status Addendum (2026-11-07)

Five P1 findings below were marked fix-before-release; all five are **RESOLVED** in the
current tree, verified against live code (issue #606). History sections are preserved
as written.

| Finding | Status | Fix mechanism (live code) |
|---|---|---|
| Redirect-chain advance capture (cookie state of a redirect hop) | **RESOLVED** | `_RedirectChain.advance` captures `served_url = self.url` BEFORE stepping to the next hop, so a redirect response's cookie state belongs to the origin that sent it (#571) — `crawlutron/transport/pinned_transport.py:201-223`; the cffi twin stores the pending hop only after the next pin admits (`fetchers.py:338-342`). |
| `render()` closing the driver on a page exception | **RESOLVED** | `BrowserRenderer._render_with_page_unlocked` wraps the render body in `try/finally` with `release_page(...)` (bounded best-effort cookie harvest → `safe_close_page` → `note_render` for ALL attempts), so a page exception releases the page without tearing down the shared driver — `crawlutron/transport/browser.py:280-288`, `browser_render_phases.py:242-257`. |
| cffi TLS-impersonation SNI (§2.5) | **RESOLVED** | The RESOLVE-table fix from the review's own sketch landed: `cffi_resolve_entry` builds the `host:port:ip` CURLOPT_RESOLVE mapping (`crawlutron/transport/pinned_transport.py:255-268`) and `_handle_cffi_hop` dials a one-shot session on the logical hostname so SNI and cert verification attest to it (#574) — `crawlutron/transport/fetchers.py:294-332`; an `SSLError` now raises visibly instead of degrading silently (`fetchers.py:323-326`). |
| namespace-scoped cookie reads (§2.2) | **RESOLVED** | The browser-cookie store is keyed by `(namespace, domain)`: `store_browser_cookies`/`get_browser_cookies_for` resolve an explicit namespace or defer to the crawl's `COOKIE_NAMESPACE` contextvar (`crawlutron/transport/browser_cookies.py:50-101`), so one caller's harvested session never surfaces to another caller's jar. |
| `/v1/keys` escalation (§2.1) | **RESOLVED** | The `/v1/keys` routes now mount on `admin_guarded_router`, so `guard_admin_request` → `get_keys_caller` requires the operator token and fails closed (401 `invalid_api_key` when unset or wrong) — `crawlutron/api/keys_router.py:18`, `crawlutron/api/route_layer.py:88-90`, `crawlutron/api/caller_auth.py:155-175` (#653, #659, #666, #676, #664); no caller key can mint, revoke, or list another caller's keys. |

---

## 1. Executive Summary

**Overall verdict: this codebase is unusually well-defended for its size, and is closer to open-source-ready than most services at this stage — but it must not ship until a short list of P1 items is fixed.** No P0 (remotely exploitable, secret-exposing, or data-destroying) vulnerability was found anywhere in 26k lines. The SSRF core — per-fetch DNS resolution with IP pinning, manual one-hop redirects re-validated per hop, fresh pins with `use_cache=False`, IP-literal de-obfuscation (decimal/hex/octal/IPv4-in-IPv6/Teredo/6to4), bounded decompression, hash-only caller-key storage, fully parameterized SQL — survived dedicated adversarial reading. That is rare and worth saying plainly.

What the review did find:

- **A privilege-escalation hole on the `/v1/keys` plane** (any valid API key can mint itself an *unbounded* key and revoke other callers' keys). One `if` fixes it.
- **Two cross-tenant isolation failures in the cookie layer** (a process-global browser-cookie store that replays one caller's session cookies into another caller's fetches, and a cookie jar that accepts `Domain=com`-style public-suffix cookies).
- **A header-value injection vector reachable on the curl_cffi transport path** (policy header *names* are banned; *values* are never checked, and libcurl will happily transmit embedded CRLF as extra headers).
- **A headline feature that silently doesn't work**: TLS-fingerprint impersonation (`HTTP_CFFI_IMPERSONATE`) can never complete a verified HTTPS handshake because the pinned-IP URL gives libcurl an IP where SNI/cert identity belongs. It fails closed, so nothing alerts you — the strategy just quietly degrades to plain httpx.
- **One genuinely quadratic regex** over attacker-controlled HTML (the module next door was rewritten into a linear scanner for exactly this class of bug — `text_from_html` reintroduced it).
- **A cluster of "silently degraded" failure modes**: sitemap failures cached for a full hour, IPv6-literal origins canonicalized into un-parseable URLs, webhook validation failures overwriting correct watch outcomes, a protocol-exception constructor that crashes on the very payloads it exists to report.
- **OSS readiness gaps that are cheap now and expensive later**: an OpenAPI/license claim of MIT with no LICENSE file in the tree, a personal-namespace repository URL wired into the default User-Agent, internal issue numbers (`#381`, `#343`…) peppered through comments that will be meaningless to the public, and ~30 orphaned half-sentence comments from aggressive refactoring.

**Duplication verdict:** the tree is already jscpd-gated and it shows — there are no large copy-pasted blocks left. The remaining duplication is *structural twins*: sync/async pairs, storage owner-filter branches repeated 7×, model↔TypedDict↔response-model triples, four spellings of the linear-ramp function, three spellings of the origin law, and a ~10-module `reset_process_state` naming family. **~700 LOC are directly removable** (dead code, dead data, dead twins) with zero wire change, and a further consolidation program (owner-fragment helper, one origin implementation, one taxonomy source, one scheduler factory) removes another few hundred while eliminating the drift points where copies have *already* diverged (the `origin_of` trailing-dot bug is a live example of duplication causing a real defect).

### Scorecard

| Area | Rating | Notes |
|---|---|---|
| SSRF / egress control | **Strong** | Pinned, fresh-resolved, per-hop re-validated; bypass hunt came up empty. Two documented residuals (browser https-subresource TOCTOU, zone-id hosts at admission) noted below. |
| AuthN/AuthZ | **Strong** | `/v1/keys` escalation closed — routes are operator-token-only and fail closed; the rest (key hashing, constant-time compares, owner-scoped reads, 404-not-403) was already solid. |
| Cookie/credential hygiene | **Two holes + leaks** | Global browser-cookie store; public-suffix `Domain=` acceptance; `Credential` repr-enabled; raw webhook URLs in logs. |
| Resource bounds | **Mostly bounded, 3 gaps** | Render options unbounded, result-store payloads unbounded, quadratic HTML regex. |
| Robots/politeness | **Correct, one split-brain** | REP matcher is spec-faithful; but the scheduler keys two entry paths by host vs registrable site, fragmenting politeness. |
| Concurrency/asyncio | **Disciplined** | Offloaded store twins, semaphore hygiene, cancellation-aware — the findings here are narrow races, not systemic. |
| Fail-open vs fail-closed | **Consistently fail-closed** | Robots, regex timeout, SSRF, providers, migrations all fail closed. The P1s here are *silent degradation*, not fail-open. |
| OSS hygiene | **Good bones, 6 gaps** | No LICENSE file; MIT claimed; personal repo URL in UA; `#NNN` comments; orphaned comment fragments; Python ≥3.12 / `regex` dep not surfaced. |
| Secrets in code | **Clean** | Zero hardcoded secrets/tokens/internal hosts across the entire tree. |

**Fix-before-release set (P1, ~2–3 days of work):** §2.1, §2.2, §2.3, §2.4, §2.5, §2.6, §3.1, §3.2, §3.3, §3.4, §3.5, §3.6 — then re-run the suite and ship.

---

## 2. Security Findings

Severity scale: **P0** exploitable/secret-exposing now (none found) · **P1** fix before release · **P2** fix soon / defense-in-depth · **P3** hardening nit.

### 2.1 [P1] `/v1/keys` is reachable by every valid API key — one request mints an unbounded key

**Where:** `crawlutron/api/caller_auth.py:104-108` (`get_keys_caller`), `crawlutron/api/keys_router.py:34-36`, `:44-48`, `:58-65`; amplifiers `crawlutron/models/key_create_request.py` and `crawlutron/api/caller_auth.py:152-165`.

```python
    if admin_token and _constant_time_eq(secret, admin_token):
        return CallerIdentity(caller_id="admin", ...)
    return await _caller_from_secret(request, secret)   # any valid caller key lands here
```

The operator check is a *prefix*, not a gate: when the presented secret is not the admin token, the dependency falls through to ordinary key verification, which succeeds for **any live caller key**. All three `/v1/keys` routes sit behind this dependency, so any key holder can:

1. **Escalate quotas**: `POST /v1/keys {"label": "x"}` — `KeyCreateRequest` defaults `max_pages=0, max_concurrency=0`, and both `resolve_page_limit` and `check_concurrency_quota` treat `0` as *unbounded* (`caller_auth.py:152-165`). `aissue_key` clamps nothing (only rejects negatives). The new key is a fresh identity, so the issuer's own quotas never bind it. Durable, too — the store persists the zeros.
2. **Revoke other callers' keys**: `revoke_key(key_id)` has no ownership check, and `GET /v1/keys` lists every key id — a one-loop cross-tenant revocation DoS.
3. **Read other callers' labels/quotas** via `list_keys`.

Verified directly: the fallthrough is exactly as quoted; the defaults are exactly as described.

**Fix (either closes it; the first is 3 lines):**

```python
async def get_keys_caller(request: Request) -> CallerIdentity:
    secret = _presented_key(request)
    admin_token = _key_admin_token(request)
    if not admin_token or not secret or not _constant_time_eq(secret, admin_token):
        raise http_error(401, "invalid_api_key", "keys plane requires the operator token")
    return CallerIdentity(caller_id="admin", authenticated=True, max_pages=0, max_concurrency=0)
```

…or, if self-service issuance is the intent: clamp `payload.max_pages/max_concurrency` to the *issuing* identity's quotas inside `issue_key`, and scope `revoke_key`/`list_keys` to `caller.caller_id`. Related (P2): with the operator-only fix, an *unset* `key_admin_token` (the settings default is `""`, `caller_auth.py:80-93` silently skips the admin branch) should hard-fail `/v1/keys` with 503 rather than silently widening access — today "no token configured" and "any key wins" are indistinguishable.

**Status — RESOLVED** (live tree, verified for this sweep; added to the Status Addendum above). The `/v1/keys` routes now mount on `admin_guarded_router` (`crawlutron/api/keys_router.py:18`), so `guard_admin_request` → `get_keys_caller` requires the operator token and fails closed — `crawlutron/api/route_layer.py:88-90`, `crawlutron/api/caller_auth.py:155-175` (#653, #659, #666, #676, #664). A presented caller key no longer passes the operator check; minting, revoking, and listing keys require `X-API-Key: CRAWLUTRON_KEY_ADMIN_TOKEN`. Cross-checked with `git log`.

### 2.2 [P1] Browser-harvested cookies are stored process-global and replayed across callers

**Where:** `crawlutron/transport/browser_cookies.py:20-43` (store keyed `("", domain)` at `:31`; harvest at `:63-66`), consumer `crawlutron/orchestration/strategy_chain.py:214-215` (cffi retry injects `get_browser_cookies_for(site)` as `Cookie`).

```python
def store(self, domain: str, header: str) -> None:
    self._store.put("", domain, _split_header(header))  # namespace is hard-coded ""


...
store_browser_cookies(url.site, header)  # keyed by registrable domain, no caller identity
```

The codebase is otherwise scrupulous about cookie partitioning — the namespaced jar pins keyed callers to `key_id` namespaces, and `effective_cookie_namespace` (`caller_auth.py:139-146`) exists precisely so one caller can't claim another's jar. **This store bypasses all of it.** A browser render that solves a challenge, runs a `fill`-based login, or merely receives `Set-Cookie: session=…` harvests *every* cookie (HttpOnly included) into a process-global LRU keyed only by registrable domain. The next job against the same site — different caller, different namespace — has that header injected into its cffi retry. Concretely: job B fetches origin A's *authenticated* content and returns it as B's result body.

**Status — RESOLVED** (live tree, per the Status Addendum). The harvest store is keyed by `(namespace, site)`: `store_browser_cookies`/`get_browser_cookies_for` take an explicit `namespace` or defer to the crawl's `COOKIE_NAMESPACE` contextvar, and `_LockedDomainCookieStore.store` puts `put(namespace, domain, ...)` — `crawlutron/transport/browser_cookies.py:52-103`. One caller's harvested session no longer surfaces to another caller's jar.

**Fix:** key the store by `(namespace, site)` and thread the caller's namespace through the harvest/retry, or delete the process-global entirely and carry the harvested header on the render result so the retry uses session-local state:

```python
def store(self, namespace: str, domain: str, header: str) -> None:
    self._store.put(namespace, domain, _split_header(header))


# harvest: store_browser_cookies(COOKIE_NAMESPACE.get(), url.site, header)
```

`InMemoryCookieStore` already supports namespaces, so this is a parameter, not a redesign.

### 2.3 [P1] Cookie jar accepts `Domain=` set to a public suffix — cross-origin cookie injection

**Where:** `crawlutron/transport/cookie_jar.py:261-267` (`_cookie_domain`), `:273` (match = `host.endswith(f".{domain}")`).

A crawled page can answer with `Set-Cookie: sid=x; Domain=com`. `_domain_matches` only checks that the attribute is a suffix of the current host — `evil.com` ends with `"com"`, so the cookie is **stored under domain `com`**. On later sends, `".victim.com".endswith("com")` is true for *every* `.com` host the crawler visits in that namespace: the attacker's cookie rides requests to unrelated origins (session fixation / credential injection against third-party sites). Browsers block this with the Public Suffix List; this jar has no such gate. For single-tenant localhost use the blast radius is "your own crawl poisons itself"; for any multi-tenant deployment it is a real injection primitive. Severity P1 for multi-tenant, P2 for the documented single-user posture.

**Fix sketch:** reject 1-label `Domain=` attributes and check the registrable-domain helper the codebase already owns:

```python
def _cookie_domain(host: str, attr: str) -> str | None:
    domain = attr.lstrip(".").lower()
    if "." not in domain:  # blocks "com", "org", "localhost"
        return None
    if registrable_domain(host) != registrable_domain(host) or registrable_domain(domain) != domain:
        return None  # Domain= must be a registrable site itself
    ...
```

(One line of `publicsuffix2`/the existing `registrable_domain` table is enough — the tree already ships a fallback PSL set in `crawlutron/models/registrable_domain.py`.)

### 2.4 [P1] Header **values** are never validated — CRLF header injection is reachable on the curl_cffi path

**Where:** `crawlutron/models/crawl_policy.py:22-41` (`_BANNED_HEADERS` bans *names* only; validator at `:205-223`), merge point `crawlutron/transport/request_profile.py:159` (`headers.update(policy.headers)`), send points `crawlutron/transport/fetchers.py:200` (cffi: `client.get(pinned_url, headers=hop_headers, ...)`) and `crawlutron/transport/pinned_transport.py:139` (httpx stream).

`CrawlPolicy.headers` is the caller's free-form outbound header surface. Names are checked against a 16-entry ban list (`authorization`, `cookie`, `host`, hop-by-hop…), but **values pass through untouched** — and the same is true of the caller-supplied conditional validators (`If-None-Match`/`If-Modified-Since`, `crawlutron/models/fetch_validators.py`, forwarded via `crawlutron/transport/http_cache.py:28-42`).

- On the **httpx** path, h11 rejects a value containing `\r\n` → the fetch fails (self-DoS only).
- On the **curl_cffi** impersonation path, the dict is lowered into a curl header list; libcurl transmits the embedded CRLF verbatim, so `{"X-Tenant": "a\r\nCookie: admin=1"}` goes out as **two headers** — and `Cookie` is precisely one of the names the ban list exists to keep callers from setting. This defeats the chokepoint by smuggling and enables request-smuggling experiments against origins.

**Fix (one guard at the read edge, one at the transport seam):**

```python
# crawlutron/models/crawl_policy.py — value validator alongside the name ban
_BAD_HEADER_VALUE = re.compile(r"[\r\n\x00]|\S{512,}")


@field_validator("headers")
@classmethod
def _check_header_values(cls, headers):  # names already banned at :205
    for name, value in headers.items():
        if _BAD_HEADER_VALUE.search(value):
            raise ValueError("invalid_policy_headers")
    return headers
```

…plus the same character check inside `apply_credentials`/`Credential` (`crawlutron/models/credential.py` — a bearer token with `\r\n` currently survives the model, which only bounds length 1–512; httpx would fail the fetch late instead of 422-ing early). Defense-in-depth: assert both name and value at `request_profile.headers_for_strategy` so any future caller of the chokepoint inherits the guarantee.

### 2.5 [P1] `HTTP_CFFI_IMPERSONATE` can never complete a verified HTTPS handshake — the flagship strategy silently degrades

**Where:** `crawlutron/transport/pinned_transport.py:115-125` (`build_pinned_request` composes the connect URL from the **pinned IP**), `:131-136` (`sni_extension` — httpx-only), `crawlutron/transport/fetchers.py:200` (cffi sends only `pinned_url`), fallback `:184` (`except Exception: return None` → plain httpx takes over).

The httpx path compensates for the IP-hosted connect URL with the `sni_hostname` request extension, so SNI and certificate identity both stay on the logical hostname — correct. The curl_cffi path has **no equivalent**: libcurl takes TLS SNI and the certificate-verification name from the URL host, which is an IP literal. libcurl omits SNI for IPs (RFC 6066) and verifies the peer certificate against the IP, which matches essentially no production cert → handshake failure → `return None` → silent fallback to httpx. Verified: no `verify=False` anywhere, so this fails *closed* — but that means the TLS-impersonation feature is dead on all normal HTTPS sites and **nothing in the system surfaces the loss** (the ladder still "succeeds" via fallback). Not a vulnerability; a silent feature-kill that also misleads anyone evaluating anti-bot posture from the strategy list.

**Fix sketch:** keep the hostname in the URL and pin the connection instead — curl_cffi accepts libcurl's `CURLOPT_RESOLVE` mapping:

```python
response = await client.get(
    str(url.raw),  # logical host in URL → SNI + cert identity correct
    headers=hop_headers,
    resolve=[f"{url.host}:{url.port or (443 if url.scheme == 'https' else 80)}:{pinned_ip}"],
    allow_redirects=False,
    stream=True,
    timeout=float(timeout_seconds),
)
```

This also makes the `sni_extension` hack unnecessary on the httpx path (unify both paths on `resolve=`), and add an observability line when the cffi attempt fails so degradation is visible (`cffi_handshake_failed` warning — today `except Exception: return None` is mute).

**Status — RESOLVED** (live tree, per the Status Addendum). The curl_cffi path now pins the connection through the pinned IP while the URL keeps the logical hostname, so SNI and certificate verification attest to the host (`PinnedRequest` carries the hostname — `crawlutron/transport/pinned_transport.py`, `crawlutron/transport/fetchers.py`; #574). TLS impersonation completes on normal HTTPS sites instead of silently degrading to httpx.

### 2.6 [P1] Quadratic regex over attacker-controlled HTML in `text_from_html`

**Where:** `crawlutron/extraction/llm_format.py:138` (and the `<style>` twin below it).

```python
without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.IGNORECASE | re.DOTALL)
```

For every literal `<script` occurrence with no matching `</script>`, the lazy `.*?` scans to end-of-string: k occurrences → O(k·n) work. A hostile page of n bytes composed of repeated `<script` makes *extraction* — on the unauthenticated crawl path — burn seconds-to-minutes of CPU per request. The irony is that this exact bug class was already found and fixed one module over: `classify.strip_html_noise` (`crawlutron/extraction/classify.py:250-277`) is a deliberate **linear** `str.find` scanner whose docstring says it exists because "it is not a backtracking regex." `text_from_html` reintroduced the pattern the neighbor was written to avoid. Only the policy `max_content_length_bytes` bounds n.

**Fix (and it is also the dedupe win — see §4):** make `text_from_html` delegate to the linear scanner:

```python
def text_from_html(html: str) -> str:
    text = html_unescape(strip_html_noise(html))  # linear, shared, capped
    return _WHITESPACE_RE.sub(" ", text).strip()
```

### 2.7 [P1] Unbounded render options → pooled-renderer occupation DoS

**Where:** `crawlutron/api/render.py:39-54` (`render_options_for` → `resolve_render_options`, no upper bound), wire model `crawlutron/models/render_options_request.py` (`timeout_seconds: int | None` with no `le=`), consumers `crawlutron/transport/browser_render_phases.py` (raw into navigation; budget only *floors* it).

The crawl path bounds the identical knob through `CrawlPolicyLimits` (`ge=1, le=MAX_HTTP_TIMEOUT_SECONDS`), but `/v1/render` accepts `options.timeout_seconds=1000000000` — the request then occupies one of a handful of pooled renderers (~700 MiB each) for as long as the target stalls, retried indefinitely. `max_retries` is likewise unbounded. On the documented small-pool deployment, one anonymous client starves every other render caller.

**Fix:** bound both knobs at `resolve_render_options` using the constants the tree already owns:

```python
_MAX_RENDER_TIMEOUT = MAX_HTTP_TIMEOUT_SECONDS  # app/vocabulary/limits
_MAX_RENDER_RETRIES = MAX_RETRIES
kwargs["timeout_seconds"] = min(int(timeout), _MAX_RENDER_TIMEOUT)
kwargs["max_retries"] = min(int(retries), _MAX_RENDER_RETRIES)
```

This is a safety bound, not a wire change: sane requests behave identically.

### 2.8 [P1] Result store accepts unbounded payloads and materializes the full table per policy-suggestions request

**Where:** `crawlutron/storage/result_store.py:30-50` (`put` — `INSERT OR REPLACE` with no byte bound), `:78-90` (`payloads()` — unbounded `SELECT payload … WHERE stored_at >= ?`), consumer `crawlutron/service/policy_suggestions.py:129` (awaited on the event loop; per-row pydantic validation also on-loop — see §3.7).

Rows are capped at 1000 *entries*, never *bytes*; payloads are full crawl responses including HTML. ~1000 multi-megabyte payloads are trivially generated (the store writes on every successful crawl, anonymous callers included), after which one unauthenticated `GET /v1/policy-suggestions` streams the whole table into RAM on the event loop — the O(n²) validation on top makes it a stuck-loop, not just a spike.

**Fix sketch:** bound at the write edge and at the read edge (parallel to `_SEARCH_RESPONSE_MAX_BYTES` which the search side already enforces):

```python
_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


async def aput(self, request_id, payload, *, owner, stored_at):
    blob = json.dumps(payload)
    if len(blob) > _MAX_PAYLOAD_BYTES:
        payload = truncated_payload(payload)  # or store a stub record
        blob = json.dumps(payload)
    ...


# payloads(): SELECT with LIMIT/batch cursor, and move validation off the loop
```

### 2.9 [P2] Admission trusts host-shaped strings the address classifier calls "not an address"

**Where:** `crawlutron/models/normalized_url.py:40-43`, `crawlutron/models/host_policy.py:125-135`, `crawlutron/models/address_class.py:113-141`.

`_host_is_public` returns True whenever `classify(host)` is `NOT_AN_ADDRESS` ("it's a name"). Two hostile families are not names either: **IPv6 zone ids** (`http://[fe80::1%25eth0]/` — the `%25` form defeats `ip_address()` at admission, while curl/httpx happily dial the zone-scoped link-local) and **control characters** (`urlsplit` strips only `\t\r\n`; a NUL or interior space survives into `host`, is classified NOT_AN_ADDRESS, and admitted — the canonical URL then carries it). The transport pin (`crawlutron/safety/validation.py:16566` re-judges the *resolved* address) is the net that catches these at fetch time, so this is defense-in-depth, not an open SSRF: the fix is cheap at the chokepoint whose docstring claims "constructing one IS admission":

```python
if "%" in host or not _HOST_OK_RE.fullmatch(host):  # [A-Za-z0-9.\-_:] minus zone percent
    raise ValueError("invalid_host")
```

### 2.10 [P2] `origin_of` and `NormalizedUrl.origin` are two implementations of one law — and they already disagree

**Where:** `crawlutron/admission/credentials.py:12-29` vs `crawlutron/models/normalized_url.py:88` (host canonicalized at `:48`, `rstrip(".")` at `:40`); third cousin `crawlutron/models/host_policy.py:11` (`_normalize_policy_host`).

`CrawlPolicy` registers credentials under `origin_of(...)`, and `apply_credentials` looks them up by `url.origin`. But `origin_of("https://example.com./")` → `'https://example.com.'` (trailing dot kept; only lowercasing + default-port dropping), while `NormalizedUrl` canonicalizes hosts with `.rstrip(".")` → origin `'https://example.com'`. **A credential registered against the dotted spelling is stored under a key no admitted URL can ever produce — silently dead, no error, ever.** The module docstring says the two decisions "must never disagree"; duplication made them disagree. Also verified: `origin_of` is not idempotent for IPv6 (`origin_of(origin_of("https://[2606:4700::1]:8443"))` raises), so validate-store-revalidate paths 422 on their own output.

**Fix:** make `NormalizedUrl.origin` the single implementation; `origin_of` becomes a thin wrapper that constructs and projects (deleting ~15 LOC), which simultaneously fixes the trailing-dot divergence and IPv6 idempotence — see §3.1 for the IPv6 half.

### 2.11 [P2] Credential model leaks secrets in `repr`; settings repr carries four live secrets

**Where:** `crawlutron/models/credential.py:11-18` (`token`/`username`/`password` are plain `Field(...)` with default repr) vs the idiom the tree already uses in `crawlutron/models/caller_key.py:10` and `crawlutron/models/issued_key.py:10` (`Field(repr=False)`); `crawlutron/config/settings.py:100-108` (`tavily_api_key`, `brave_api_key`, `serper_api_key`, `key_admin_token` as plain dataclass fields).

Grep found **no active leak** — no call site formats a `Credential` or the settings object today. But the model docstring promises "never logged or echoed," and one future `logger.debug("%r", policy)` or error payload prints bearer tokens, basic-auth passwords, and every provider key. This is the cheapest insurance in the report:

```python
token: Annotated[str | None, Field(min_length=1, max_length=512, repr=False)] = None
# settings.py
tavily_api_key: str = field(default="", repr=False)
```

### 2.12 [P2] Webhook URLs are logged raw; the anonymous watch bucket shares everything

**Where:** `crawlutron/service/webhooks.py:146,159` (`extra={"url": webhook_url, ...}` — raw caller string, frequently carrying `?token=…` or userinfo secrets); `crawlutron/api/watch_router.py:87,100,112` (all watches scoped strictly by `caller_id`, and un-keyed callers share `caller_id=""`).

Two compounding issues: (a) the delivery logs persist receiver URLs verbatim — in practice these embed bearer tokens, so the *log* becomes the leak channel; note also that userinfo is silently dropped by canonicalization before delivery, so the secret is logged **and** never used. (b) Every un-keyed caller owns every other un-keyed caller's watches: `GET /v1/watches` discloses webhook URLs (token disclosure), `DELETE` lets one anonymous client kill another's monitoring, and the 100-watch cap is shared. Under the README's "localhost only" posture this is acceptable-by-design; under "reverse proxy in production" with un-keyed tenants it is not. Fix (a) by logging `host`/canonical raw only; mitigate (b) by refusing to serialize `webhook_url` in list responses and documenting the bucket collapse in SECURITY.md.

### 2.13 [P2] Webhook signature doesn't cover the timestamp; 3xx counts as delivered

**Where:** `crawlutron/service/webhooks.py:92-95` (HMAC over body only; `X-Crawlutron-Webhook-Timestamp` sent unsigned), `:87-91` (`>= 500 or == 429` retryable, `>= 400` permanent — everything else, including `301/302/307/308`, falls through to *success*).

A captured `(body, signature)` pair replays forever with any timestamp — the timestamp header is decorative. And because `follow_redirects=False` (good for SSRF), a receiver that 30x-redirects (http→https misconfig, proxy) is recorded as *notified* while nothing arrived — for a monitoring feature, "delivered" that isn't is the worst failure mode. Both are outbound-contract changes, so land them **before** the API freeze:

```python
digest = hmac.new(secret.encode(), f"{ts}.".encode() + payload_bytes, hashlib.sha256).hexdigest()
# and: if 300 <= status < 400: raise DeliveryFailed("webhook receiver redirected", retryable=False)
```

### 2.14 [P2] Hostile-page XHR bodies parsed before the size cap (browser path)

**Where:** `crawlutron/transport/browser_targets.py:183-191` — `response.json()` materializes the whole body, then `len(json.dumps(payload))` builds a second full copy just to measure it against the 8 KB cap. A page issuing a 1–2 GB JSON XHR spikes the shared pool browser's host process. Cap via `response.body()` byte-limit before parse (or honor `content-length` first), and drop the dump-for-length (serialize the capped excerpt once).

### 2.15 Checked clean — the SSRF core and its neighbors

For the record, these were hunted hard and held (each verified in source, not taken on faith):

- **IP-literal obfuscation** — decimal `2130706433`, hex `0x7f000001`, octal `0177.0.0.1`, short quads `127.1`, glibc wrap; IPv4-mapped `::ffff:…`, 6to4, Teredo all classified and refused (`crawlutron/models/address_class.py:51-96`).
- **DNS pinning** — fresh resolve per pin (`use_cache=False` everywhere it matters), pinned IP is what's dialed, `Host` forced from the admitted URL, per-hop re-pin across manual redirects, full addrinfo list checked (not just the first record) (`crawlutron/transport/pinned_transport.py:42-102, 247-293`).
- **Credential stripping on redirect** — origin-equality includes scheme (https→http strips too); `cookie`/`authorization`/`proxy-authorization` dropped on cross-origin hops; hop `Set-Cookie` captured before moving on (`pinned_transport.py:212-226`).
- **Search provider keys** — keys live only in memory and flow to three constant URLs; the pooled client refuses redirects outright; response bodies capped at 1 MiB (`crawlutron/search/providers.py`, `pooled_clients`).
- **SQL** — every value bound, identifiers through an `[A-Za-z0-9_]` allowlist, DDL through quoted constants (`crawlutron/storage/sqlite_store.py:125-128`, `ddl.py`).
- **Caller keys** — 256-bit `ck_` tokens, SHA-256 *hash* storage with UNIQUE index (no plaintext anywhere; lookup-by-digest is fine for high-entropy tokens), secret returned once, `compare_digest` for the one admin-token compare, no key material in any log/error (`crawlutron/storage/caller_key_store.py`, `crawlutron/api/caller_auth.py:94-101`).
- **Migration runner** — process lock + yoyo DB lock, stamped pre-ledger only after drift check, fail-closed `_require_current_schema` (`crawlutron/storage/migrations/runner.py:82-107`).
- **Bounded IO/decompression** — every decompression call site passes a cap (required kwarg), brotli refuses to run uncapped, declared Content-Length pre-gate plus streamed cap (`crawlutron/transport/decompression.py:31-67, 147-148`).
- **MCP** — 16 MiB body cap, `extra="forbid"` models, bounded tool payloads, notifications drained before header validation, generic "Internal error" on the wire (`crawlutron/mcp/server.py`, `crawlutron/api/middleware.py`).
- **Browser navigation** — route guard wired before any navigation (including warmups), WebSockets fail closed, downloads never enabled, no CDP surface, `file://`-class schemes rely on browser sandbox but are at least *continued* rather than aborted (P3: abort everything non-http would be strictly safer) (`crawlutron/transport/browser_targets.py`, `browser_render_phases.py:72-85`).

---

## 3. Correctness Findings

### 3.1 [P1] IPv6-literal URLs canonicalize into strings no HTTP client can parse

**Where:** `crawlutron/models/normalized_url.py:48` (`netloc = host if port is None else f"{host}:{port}"`, built from the bracket-stripped `parsed.hostname`), consumers across the tree.

`http://[2606:4700::1111]/` → host `"2606:4700::1111"` → canonical `http://2606:4700::1111/`. Re-parsing that yields hostname `"2606"` and an invalid port — urlsplit raises, as do httpx/curl. With an explicit port it's worse: `http://[2606:4700::1111]:8443/` → `http://2606:4700::1111:8443/`, which a lenient parser reads as a *different address*. Downstream, verified end-to-end:

- `fetch_job_sitemap_candidates` (`crawlutron/admission/admission.py:236`) builds `{origin}/sitemap.xml` → re-admission raises → **every IPv6 origin silently gets no sitemap seeding**.
- The robots gate resolves `{origin}/robots.txt` the same way → `RobotsSource.unreachable` → **disallow-all for every IPv6 literal** under `respect_robots`.
- Credential origin keys and the robots cache key inherit the ambiguous string.

Fail-closed throughout, so not exploitable — but IPv6 support is de-facto absent while `address_class` deliberately handles NAT64/ULA/Teredo, i.e. IPv6 was *meant* to work. The transport already knows the fix (it re-brackets pinned IPs, `pinned_transport.py:24846-24848`):

```python
netloc = f"[{host}]" if ":" in host else host
netloc = netloc if port is None else f"{netloc}:{port}"
# and the same bracketing inside the `origin` property
```

One edit heals sitemap seeding, robots, credentials, and `origin_of` idempotence (§2.10) together.

### 3.2 [P1] Sitemap walk caches failure/emptiness for a full hour

**Where:** `crawlutron/orchestration/fetch_helpers.py:255-268` (`fetch_sitemap_urls`), `:220-221` (`_recurse_sitemap` early-returns on `text is None`), `:93` (`fetch_decode_discovery` suppresses *every* exception), `:193-196` (45 s fanout timeout returns partial sets).

```python
try:
    root_etag, root_lm = await _recurse_sitemap(walk, sitemap_url, 0, conditional)
except Exception:
    root_etag, root_lm = (stale[1] if stale else None), (stale[2] if stale else None)
SITEMAP_CACHE.put(cache_key, (list(page_urls), root_etag, root_lm))  # unconditional
```

An empty result is not `None`, so it's served as *fresh*; the `put` runs even when the walk raised, stalled, hit a decompression cap, or produced a partial fanout. `SITEMAP_CACHE_TTL_SECONDS = 3600`. One transient network blip — or a hostile origin that stalls its first sitemap response, or a gzip bomb → `DecompressionTooLargeError` → `text is None` — silently disables sitemap seeding for that origin for an hour. Contrast the robots module next door, which has a 60 s failure TTL *and* single-flight; sitemaps have neither.

**Fix sketch:** don't cache nothing — either skip the `put` when `not page_urls and root_etag is None and root_lm is None`, or store with a short failure TTL mirroring `ROBOTS_FAILURE_TTL_SECONDS`, and add a `_inflight` single-flight keyed like `crawlutron/transport/robots.py:98`:

```python
if not page_urls:
    SITEMAP_CACHE.put(cache_key, ([], None, None), ttl=ROBOTS_FAILURE_TTL_SECONDS)  # 60 s
else:
    SITEMAP_CACHE.put(cache_key, (list(page_urls), root_etag, root_lm))  # normal TTL
```

### 3.3 [P1] The scheduler keys two entry paths by different identities — politeness split-brain

**Where:** `crawlutron/orchestration/recursive.py:258` (`make_admitted_task`: `domain=link.normalized_url.host`) vs `:278` (`_seed_recursive_frontier`) and `crawlutron/orchestration/request_pipeline.py:91` (`_build_jobs`: `domain=normalized_url.site`). Three docs agree the key must be the registrable site: `CrawlTask` (`crawlutron/models/crawl_task.py`), `ScheduledJob.domain` (`scheduler.py:14387`), `NormalizedUrl.site` (`normalized_url.py:10624`).

The scheduler keys **everything** on `job.domain`: per-domain semaphore, token bucket, circuit breaker, cooldowns (including the robots Crawl-delay floor committed by `admission_links._admit_chunk` under the **host** key), adaptive EMA, strategy pins. Seed `https://example.com/a`; it links to `https://www.example.com/b` → two gates for one site: `per_domain_concurrency` effectively doubled, the robots Crawl-delay floor set under one key ignored by the other, 429-driven `set_domain_cooldown` bypassed by the sibling key, strategy pins missed. That is a robots-compliance violation with teeth, not a bookkeeping nit.

**Fix:** one line — `domain=link.normalized_url.site` in `make_admitted_task` — plus a test asserting key equality across the three entry paths.

### 3.4 [P1] `ProtocolValidationError` crashes with `UnboundLocalError` on the payloads it exists to report

**Where:** `crawlutron/mcp/protocol.py:22-29`.

```python
def __init__(self, status_code: int, payload: JsonValue) -> None:
    if isinstance(payload, dict):
        ...
        message = error.get("message", "validation error")
    super().__init__(str(message))  # message unbound when payload isn't a dict
```

`payload: JsonValue` explicitly allows non-dicts; the router's 400 path depends on this exception carrying a message. Latent today (all current callers pass error dicts) but it is one caller away from masking a real protocol error with a confusing crash. One line: `message = "validation error"` before the branch. (`result_response` at `:36-41` also mutates the caller's dict via `setdefault("_meta", ...)` — copy first.)

### 3.5 [P1] Webhook pre-validation raises outside the retry loop — corrupts correct watch outcomes

**Where:** `crawlutron/service/webhooks.py:149` (`await validate_webhook_url_async(...)` *before* the attempt loop), consumer `crawlutron/service/watch_sweep.py:145-169` (check outcome recorded first, webhook fired after), blanket handler `:104-120`.

`validate_webhook_url_async` raises on transient DNS failure, resolver-budget timeout, and host-policy drift. In the watch sweep the *correct* check outcome has already been recorded; the raise propagates to the sweep's blanket handler, which re-records the watch as `unknown_error`, increments `consecutive_failures`, and drops the change notification — one DNS blip at delivery time turns a good check into a failed one, silently. The same raise escapes into `_publish_success` after a job already won its COMPLETED CAS (`crawlutron/service/async_crawl_runner.py`). And the pre-loop validation is redundant anyway: `deliver_pinned_webhook` re-resolves fresh and fails closed per attempt.

**Fix:** delete the pre-loop validation (it's simultaneously the bug *and* a per-delivery DNS round-trip — see §5.3 item 5) and let the per-attempt pin path do its job. If you keep it, wrap the delivery await in the sweep.

### 3.6 [P1] `start_tracked_task`'s done-callback can untrack a *newer* task

**Where:** `crawlutron/service/job_lifecycle.py:51-54`.

```python
task = asyncio.create_task(factory())
_tasks[job_id] = task
task.add_done_callback(lambda t: _tasks.pop(job_id, None))  # pops whatever is there now
```

Task A finishes → its callback is scheduled via `call_soon` → before it runs, `resume_job` registers task B for the same job_id → A's callback deletes **B's** registration. B now runs untracked: shutdown won't cancel it, `is_running` can't see it, and the next boot treats its RUNNING row as orphaned and starts a second runner. Reachable via pause→quick-resume of a whole-site job.

```python
    task.add_done_callback(lambda t: _tasks.pop(job_id, None) if _tasks.get(job_id) is t else None)
```

### 3.7 [P2] `/v1/policy-suggestions` validates and rolls up unbounded rows on the event loop

**Where:** `crawlutron/service/policy_suggestions.py:101-110` (`_valid_traces` — pydantic `model_validate` per row, on-loop), `:34-65` (per-page rollup with an O(n²) signature dedupe), fed by `result_store.apayloads()` (unbounded — see §2.8). One GET against a large retention store stalls the loop for seconds-to-minutes. Fix: move validation+rollup inside the `to_thread` offload, cap the signature set, and bound the record window (§2.8's cap is the real fix).

### 3.8 [P2] Background-job lifecycle: zombies, queue-lies, and held semaphores

- **Crash-orphaned PENDING single-shots are permanent zombies** — `crawlutron/service/job_recovery.py:44-45` skips every PENDING row whose kind isn't `whole_site`; nothing ever retires them, so `GET /v1/crawl/{id}` answers 202 "still pending" forever and the webhook never fires. Fail them in recovery the way RUNNING non-resumables are failed (`:29`).
- **Async jobs claim RUNNING before the semaphore** — `crawlutron/service/async_crawl_runner.py:70-77`: under a flood of 202s, jobs sit `running` doing nothing (and consume concurrency quota while queued). Claim after acquiring, or add a QUEUED state.
- **Webhook delivery with retries holds the crawl semaphore** — same file, the retry loop (up to ~60 s/attempt backoff) runs inside `async with _get_async_crawl_semaphore()`; one slow receiver starves crawl width. Move the POST outside the guarded block.
- **Concurrency-quota TOCTOU on async doors** — `crawlutron/api/caller_auth.py:198-203` + `crawl_endpoint.py:138-142`/`jobs_router.py:88-89`: for `reserve_slot=False` the check→insert window contains awaits; two concurrent submits at `max_concurrency=N` both pass and both create. Reserve a tracker slot for async doors and release it at terminal, or make the quota check-and-insert one transaction.

### 3.9 [P2] Extraction subsystem: races, re-reads, and one silent charset bug

- **`_suppress_trafilatura_chatter` is thread-unsafe and can permanently silence ERROR logs** — `crawlutron/extraction/documents.py:222-231` snapshots/restores a process-global logger level from concurrent `to_thread` workers; B snapshots A's suppressed CRITICAL and "restores" it forever. Use a module-level refcount.
- **Per-extraction sysfs I/O via eager default args** — `crawlutron/config/settings.py:284,287`: `default_http_max_connections()` (cgroup file reads) runs on **every** `CrawlerSettings.from_mapping()`, and the extraction paths (`documents.py:135,275`) re-derive settings per page because callers don't thread `settings` through `extract_html`. Cache the process-level default once and pass settings down.
- **Document output capped after the pipe transfer** — `documents.py:300` caps markdown in the parent, but the worker already sent the full string across the multiprocessing pipe (`doc_pool.py`): a bomb PDF is materialized twice. Cap in the worker before `send`.
- **4-slot selector pool permanently starvable** — `documents.py:114-118`: `soup.select()` can't be cancelled, so 4 hostile page+selector pairs hang the global pool forever (every later selector eval service-wide times out). Bound element counts before submit, or run selectors in the disposable doc subprocess.
- **Charset ignored in the FULL extraction path** — `crawlutron/extraction/content.py:62-64` decodes `utf-8, replace` regardless of declared charset, while the TRAFILATURA path receives transport-decoded text: one body, two decodes, windows-1252 pages yield mojibake markdown. Thread the transport decode through (also fixes the BOM sniff miss — use `utf-8-sig`).

### 3.10 [P2] Runtime/scheduler: silent probe death, commit storms, unpinned retries

- **LoopLagProbe dies permanently on first exception** — `crawlutron/runtime/pressure_probes.py:56-69`: one transient error sets `_lag=0.0` and never restarts the tick; the gate then sheds nothing, silently. Log + restart, or mark the fallback.
- **Metrics commit per counter, per request** — `crawlutron/runtime/metrics.py:58-62,110-111,144-147`: each `_add` is its own SQLite upsert **+ commit**, and caller-tagged observations additionally snapshot the whole counters table to count distinct series. Under load, request completion serializes on SQLite fsyncs. Batch deltas into one transaction; keep the distinct-caller set in memory.
- **Cookie-retry success never pins the strategy** — `crawlutron/orchestration/strategy_chain.py:214-215,305-307`: domains needing the HTTP→browser→cookie-harvest→cffi flow re-climb the whole ladder every page. Pin on the retry-success return.
- **`truncate_html_input` counts chars, not bytes** — `crawlutron/safety/bounded_io.py:22,29-30`: astral-heavy pages occupy up to 4× the declared 4 MiB bound. Count UTF-8 bytes or rename honestly.
- **Mixed wall/monotonic clocks in one class** — `crawlutron/transport/scheduler.py:154,313,227`: NTP steps extend/erase cooldowns; store durations from one monotonic source. Related P3s: four per-domain dicts never purged (safe today because schedulers are per-job — pin that contract in a comment or extend `purge_expired`), and `CircuitBreakerOpen`'s message embeds a raw epoch float.

### 3.11 [P2] Browser pool: dead liveness guard, cancellation tab-leak, self-duplicating init

- **`is_closed` guard is a no-op on real Playwright objects** — `crawlutron/transport/browser_driver.py:178`: `getattr(self._browser, "is_closed", False)` — `Browser` has no `is_closed` attribute (verified against Playwright 1.57; only `is_connected()` exists), so a crashed browser is never detected here; recovery waits for the next `new_page()` to raise. Use `not self._browser.is_connected()`.
- **Cancellation leaks the page and skips the recycle count** — `crawlutron/transport/browser_render_phases.py:197-205`: `except TimeoutError` doesn't catch `CancelledError`, so a cancelled harvest skips `close_page`/`note_render` and the tab stays open on the pooled browser. Catch `BaseException`, close, re-raise.
- **`initialize()` partial failure duplicates renderers** — `crawlutron/transport/browser_pool.py:54-68`: an exception mid-loop leaves k renderers queued with `_initialized=False`; the next `acquire()` appends a full pool again — live renderers exceed the RAM-derived size. Clear queues at the top of the locked init body.
- **SSRF-refused targets are retried, terminally wasteful** — `crawlutron/browser.py:67-75`: a policy refusal (`ValueError` from `resolve_browser_target`) maps to `retry_exhausted` and re-renders `max_retries+1` times, relaunching a ~700 MiB browser each time. Classify refusals terminal on attempt 1. Related: `run_pre_extraction_chain`'s challenge wait (up to 300 s) sits outside the render budget; `pool.close()` closes leased renderers mid-render (shutdown noise only — release-after-close is correctly guarded).
- **`Expires` parsed in local timezone** — `crawlutron/transport/cache_control.py:56-65`: `parsedate_to_datetime` yields naive datetimes; `.timestamp()` applies local TZ, so with `TZ≠UTC` every Expires-derived TTL skews by the offset (can go negative → not cached). Use `parsedate_tz` + `mktime_tz`.

### 3.12 [P2] HTTP fetch path: no total deadline, brotli truncation, dead JS

- **No total deadline across hops or body reads** — `crawlutron/transport/pinned_transport.py:182` caps hops at 5 but each hop gets a fresh full timeout and body reads are per-chunk; a hostile origin trickling 1 byte/9 s holds a fetch open indefinitely, ×6 hops. Track `deadline = started + k * timeout_seconds`, check per chunk/hop, raise `TimeoutError` past it.
- **Truncated brotli decodes silently** — `crawlutron/transport/decompression.py:126-136,148-150`: the drain loop returns partial output for a truncated stream; gzip/deflate raise on the same corruption. Check the decoder's eof/finished in both brotli branches.
- **Consent-fallback JS is a `SyntaxError`** — `crawlutron/transport/driver_pages.py:95-96`: a newline inside a single-quoted JS string makes `page.evaluate` throw, swallowed by `except Exception: return False` — the text-based fallback has never run. Join the literal.
- **Retry policy re-downloads deterministic failures** — `crawlutron/transport/fetch_policy.py:45-58` retries every exception including `DecompressionError`; non-retryable classes should short-circuit. Related P3s: compressed `content-length` kept post-decompress (`fetchers.py:135`), 401/407 unmapped in `http_classify.py:80-87`, `Retry-After` HTTP-date form ignored.

### 3.13 [P2] Storage: one corrupt row kills the watch sweep; listing trusts `stats`

- **`watch_store` has zero corrupt-row tolerance** — `crawlutron/storage/watch_store.py:119-129,78-98,212-229`: `ContentValidators.model_validate_json` / `CrawlClassification(...)` raise on a value a sibling version left; `due()` then raises every sweep cycle (watch checking dead process-wide) and `/v1/watches` 500s. `job_store` guards the identical hazard (`job_store.py:193-197`) — copy that pattern (per-row skip+warn).
- **`_status_row`'s `json.loads(row["stats"])` is unguarded** — `crawlutron/storage/job_store_read.py:168`, consumed by list/status paths: one corrupt column 500s the whole listing while the row-load path treats the same corruption as a logged miss. Same fix.
- **Migration stamp/rollback fabricate an empty memory DB** — `crawlutron/storage/migrations/runner.py:108-132` calls `_locked_backend` without `memory_name`; latent (CLI is file-path only) but a trap for in-process use.
- **Watch-cap check-then-create isn't atomic** — `watch_store.py:100-103` + `:31-77`; concurrent creates exceed `max_watches_per_caller`. Offer `create_bounded(owner, cap)` doing the count and insert under one lock.
- **Provider JSON misses `RecursionError`** — `crawlutron/search/providers.py:169-172`; a deeply-nested ≤1 MiB body 500s instead of the 502 envelope. Also truncate provider-controlled `detail` before echoing it into the envelope (`:176-180`, currently up to ~1 MiB of reflection).

### 3.14 [P2] Robots/cap semantics: budget spent on denials, stall mislabeled

- **Robots-denied URLs consume the page cap** — `crawlutron/orchestration/recursive.py:145-147` increments `page_count` on denials; the nonrecursive path (`request_pipeline.py:289-308`) doesn't. A robots-heavy site under-delivers against `max_pages`, and the two paths disagree on what the cap means. Don't count denials toward the cap (or document that it does).
- **Per-result full-set copy of `known`** — `recursive.py:336-337` re-copies the occupied set after every expanded result: O(results × links). Have admission mutate the loop-owned set in place or return a delta.
- **Spill write still walks the result on the loop** — `engine.py:269`: the dict literal + `to_dict` (multi-MB HTML walk) runs before `to_thread`; wrap the whole lambda. And **spill GC is a synchronous tempdir scan inside async code** (`:228-235`) on attacker-seedable file counts — `to_thread` it.
- **Stall fallback mislabels resource stalls as `browser_pool_exhausted`** — `crawlutron/orchestration/admission_stall.py:57-58`: dashboards will blame the renderer pool for CPU/memory stalls. Pass the actual gate.
- **Unknown exceptions default to transient `network_error`** — `crawlutron/transport/classification.py:68`: a strategy bug is retried 3× per URL as if the host were flaky. Consider a distinct `internal_error` classification (taxonomy-gated).

### 3.15 Checked clean — the hard parts

Verified sound under adversarial reading (so you don't have to re-litigate them):

- **Robots fail-closed matrix** — 401/403/429 → Disallow-all (deliberately stricter than RFC 9309, documented); 5xx/transport → Disallow-all with 60 s TTL; 8 MiB cap failure → closed; cache keyed `(origin, HostPolicy)` with per-entry TTL and single-flight; every re-read URL re-admitted against the live policy (`crawlutron/transport/robots.py:57-98`, `serialization.py:60-70`).
- **REP matcher** — `*`/`$` semantics, path+query matching, longest-match with Allow winning ties, empty patterns skipped, dual bounded LRU pattern cache (`robots.py:106-203`). Two spec-fidelity P3s only: group selection is substring-in-token (`robots_parse.py:65`) and per-directive `accepts` changes group boundaries (`:70-75`).
- **Sitemap egress** — every `<loc>` re-admitted via `resolve`+`admit_or_none`; recursion capped (depth 3, 10k URLs); regexes linear.
- **Crawl-loop bookkeeping** — transient requeue bounded (3) and visited-discarding; shutdown restores popped jobs to the frontier in order; `save()` is INSERT-only for `status`/`owner` so control-plane state can't be clobbered; accumulator graft on reload correct; doc-pool acquire/release paired with retire-and-reap.
- **Asyncio discipline** — store twins all offloaded (`atwin`/`to_thread`); `CancelledError` re-raised where it matters (`run_job`, worker `execute`, atomic_write's `BaseException` cleanup); fire-and-forget webhooks tracked and drained on shutdown; event-loop singletons rebuilt synchronously (no await between check and return).
- **Middleware** — hostile-Origin rejection admits non-browser clients and loopback only; body cap drains then 413s with a correct truncation latch; no `X-Forwarded-For` trust anywhere.
- **Pydantic discipline** — `mode="before"` vs `after` used correctly (credentials canonicalizer rewrites keys before typing); `default_factory` everywhere; `extra="forbid"` on request surfaces; lru_caches keyed to bounded inputs.
- **Lifespan** — startup order documented and race-commented; shutdown event minted in lifespan, not import; sampler detached before pool close.

---

## 4. Duplication Inventory & LOC-Reduction Program

**Honest headline first:** this tree is already jscpd-gated (there's a `jscpd_report` model and a duplication-budget test in the repo), and it shows — the classic "same 40-line function pasted in five files" does not exist here. What remains is **structural duplication**: twins born from sync/async pairing, storage boilerplate, model↔payload↔response triads, and the same one-law-three-spellings pattern. That's also where the *bugs* hide: two of the findings above (§2.10's credential dead-key, §3.3's politeness split) are direct consequences of copies that drifted.

LOC figures below are estimates verified by reading; "dead" items were grep-confirmed to have zero app-side callers (re-check against your test suite before deleting — the concatenated upload excludes `tests/`).

### 4.1 Dead code — delete outright (~330 LOC)

| # | Item | Location | LOC |
|---|------|----------|-----|
| D1 | `build_unified_crawl_response` (sync twin; only the async twin is called) | `crawlutron/service/response_shaping.py:70-81` | ~12 |
| D2 | `validate_webhook_url` (sync twin; only async used) | `crawlutron/service/webhooks.py:179-185` | ~7 |
| D3 | `aformat_crawl_item` (no callers; `format_crawl_item` used synchronously inside an already-offloaded builder) | `crawlutron/service/unified_shaping.py:46-51` | ~7 |
| D4 | `render_options_for`'s dict arm (only caller always passes `RenderRequest`; `RenderOptionsRequest` is `extra="forbid"`) | `crawlutron/api/render.py:43-54` | ~8 |
| D5 | `_depends_singletons.py` — whole module re-declares `Depends(get_settings)` that typed Annotated deps already provide (and loses the annotation) | `crawlutron/api/_depends_singletons.py` | ~12 |
| D6 | `tracked_task` (docstring promises a supervisor seam; zero callers) | `crawlutron/service/job_lifecycle.py:13-17` | ~5 |
| D7 | Dead pre-validation block in `execute_crawl` (calls the engine that re-validates identically) | `crawlutron/service/crawl_executor.py:38-48` | ~10 |
| D8 | `reset_process_state` in async runner (no repo caller) | `crawlutron/service/async_crawl_runner.py:47-55` | ~8 |
| D9 | `HOST_POLICY_ENTRY_POINTS` (declared, exported, zero consumers) | `crawlutron/models/host_policy.py:57-78` | ~22 |
| D10 | `FrameCollector` + re-exports (zero callers; "back-compat" for an internal API) | `crawlutron/extraction/trees.py:75-95` + `content.py` exports | ~25 |
| D11 | `parse_count` tally machinery (zero callers; a lock on the hot parse path exists only for it) | `crawlutron/extraction/trees.py:34-52` | ~12 |
| D12 | Dead engine alias re-exports (`RobotsMatcher`, `load_robots_rules`, `cached_sitemaps`) | `crawlutron/orchestration/engine.py:45-48` | ~4 |
| D13 | `_MatchAll` + empty-pattern branch (unreachable: `robots_allows` skips empties first) | `crawlutron/transport/robots.py:177-183` | ~6 |
| D14 | `own_session` / `owns_process_state` (zero callers) | `crawlutron/transport/pooled_cffi.py:54-64` | ~7 |
| D15 | `_swap_cffi_builder` (zero callers; `pooled_client_builders(cffi=…)` is the used path) | `crawlutron/transport/http_client_pool.py:29-38` | ~10 |
| D16 | `CrawlResultStore.get`/`aget` (unfiltered reads; no callers) | `crawlutron/storage/result_store.py:51-55,94` | ~10 |
| D17 | `WatchStore.list_watches`/`alist_watches` (route uses the owner-scoped twin) | `crawlutron/storage/watch_store.py:89-91,206` | ~8 |
| D18 | 11 pure-delegation methods on `BrowserDriver` → the module functions they wrap | `crawlutron/transport/browser_driver.py:157-170,210-214,288-293,303-308` | ~32 |
| D19 | `browser.py` re-export shim (external code imports only `BrowserRenderer` + `get_browser_cookies_for`) | `crawlutron/transport/browser.py:19-58` | ~10 |
| D20 | Settings TypedDict boilerplate — six `_XFields` TypedDicts exist only so `from_mapping` unpack type-checks | `crawlutron/config/settings.py:128-186` | ~50 |
| D21 | `well_known.recommended_endpoints` (hand-kept restatement of the `links` dict) | `crawlutron/discovery/well_known.py:26-48` | ~18 |
| D22 | `_BC_OWNED_HANDLER` (written 3×, never read) + dead `except ValueError` around never-raising `get_env_int` + dead `or "."` fallbacks | `crawlutron/runtime/logging_config.py:72,101,115`; `crawlutron/runtime/resource_pressure.py:44-47`; `crawlutron/safety/atomic_write.py:15-19` | ~8 |
| D23 | Dead third reserved-membership re-check (unreachable; would miscount `duplicates_skipped` if revived) + half-dead `except (UnsafePattern, re.error)` (`compile_untrusted` already wraps `regex.error`) + duplicate `_MAX_SCOPE_PATTERN_LEN` (= `safe_regex` default) | `crawlutron/admission/admission_links.py:167-168, 269-270, 33+5` | ~6 |
| D24 | Dead key-length recheck (pydantic `max_length` already fired) | `crawlutron/models/key_create_request.py:12` | ~2 |

**Sum: ~300–330 LOC**, zero behavior change, wire frozen.

### 4.2 Structural twins — consolidate (~350–420 LOC + drift-proofing)

| # | Twin | Sites | Sketch | Est. saved |
|---|------|-------|--------|-----------|
| T1 | **Owner-filter branch** `if owner is None: (no WHERE) else: AND owner = ?` repeated across every read method | `job_store.py` ×5, `result_store.py` ×1, `watch_store.py` get/list/delete ×4 | One `where_owner(owner) -> tuple[str, params]` fragment helper; each site shrinks to a suffix + param append | ~45 |
| T2 | **Sync/async response twins** | `response_shaping.py:177-196`; `content_cache.py:272-331` (`set`/`aset`) | Shared core + one-line adapters (`_aresponse(result, adapt)`; `_commit_written(...)` tail) | ~35 |
| T3 | **Payload-TypedDict ↔ response-BaseModel triads** — every field written twice, drift undetected | `agent_hints.py:95-112`↔`agent_hints_response.py`; `capabilities.py:39-56`↔`capabilities_response.py`; `well_known.py:5-58`↔`discovery_response.py`; `DiscoveredLink`↔`DiscoveredLinkResponse` | Build the BaseModel once (or `model_validate(payload)`); `DiscoveredLinkResponse.from_record(link)` (pattern exists in `KeySummary`) | ~80 |
| T4 | **Taxonomy spelled twice** — 36-member `Classification` Literal vs `CrawlClassification` StrEnum; 6-member `CrawlStatus` vs `ResultStatus` | `crawlutron/models/taxonomy.py` vs `crawlutron/vocabulary/taxonomy.py`; `crawlutron/models/crawl_status.py` | StrEnum is the source; derive the Literal (`Literal[tuple(CrawlClassification)]`) + runtime gate test. Kills the two-hand-edit-per-new-classification drift class | ~50 |
| T5 | **Origin law, three spellings, already diverged** (§2.10) | `credentials.origin_of`, `NormalizedUrl.origin`, `host_policy._normalize_policy_host` | `NormalizedUrl.origin` canonical; others project through it | ~15 |
| T6 | **Scheduler-from-settings, 3 spellings, one already missing knobs** (render's copy omits `max_rps`/adaptive) | `render.py:165-171`, `admission.py:65-78`, site crawler | `scheduler_from_settings(settings, **overrides)` in `crawlutron/transport/scheduler.py` | ~20 |
| T7 | **Robots decision-application loop** — same zip→spill-denied→set-delay→collect skeleton | `request_pipeline.py:291-308` vs `recursive.py:124-153` | `apply_robots_decisions(jobs, decisions, *, on_denied, on_delay)` | ~20 |
| T8 | **Terminal-result constructors** — `result_factory` re-spells attempt kwargs `terminal_attempt` owns | `result_factory.py:143-151` vs `results.py:121-171` | Route attempt assembly through `terminal_attempt` | ~10 |
| T9 | **Fetch+decompress+decode discovery pipeline, 3rd spelling** | `orchestration/fetch_helpers._call_fetcher`, `transport/fetch_policy._fetch_robots_text`, document fetch tail | Consolidate on `fetch_helpers` | ~15 |
| T10 | **HTTP terminal-hop stream readers** — length pre-gate → advance → capped read → finish, duplicated for httpx vs cffi chunk sources | `fetchers.py:67-99` vs `:186-232` | One `terminal_hop(read_chunks: AsyncIterator[bytes])`; cffi queue becomes an async generator | ~50 |
| T11 | **Provider URL constants re-declared** (canonical copies feed the `PROVIDER_ENDPOINTS` trust set) | `providers_api.py:19-21`, `providers_html.py:17-18` vs `providers.py:25-29` | Import from `providers` | ~6 |
| T12 | **Cookie-slot upsert SQL duplicated** | `coordination_cookies.py:58-62` vs `:103-108` | `_upsert_slot(key, payload, now)` | ~5 |
| T13 | **Transient-retries coercion run twice per load** + **root re-admission duplicated** + **root-folds-to-depth-0 twice** | `job_store_read.py` vs `job_store_state.py`/`job_store_collections.py` | Call each helper once in the row path | ~11 |
| T14 | **Linear ramp ×4, deadline wait-loop ×2** | `memory_pressure.py:46-59,112-131`; `pressure_probes.py:72-79`; `resource_pressure.py:165-171`; `pressure_gates.py:157-164` | `ramp(value, one_at, zero_at)` + `_wait_below(predicate, clock, deadline, poll)` | ~20 |
| T15 | **`_raise_not_found` twins; denial-response splats; inline `Tracking(...)`; duplicated 401 raise** | `watch_router.py:26-27`/`jobs_router.py`; 6 route declarations; 3 routers; `caller_auth.py:105` | One `not_found()` factory in `api.exceptions`; router-level `responses=`; take `TrackingDep` | ~15 |
| T16 | **`JobStore` init/close scaffolding ≈ `CoordinationStore`** | `job_store.py:58-85,338-341` vs `coordination_store.py:24-39` | Subclass (also centralizes `row_factory = sqlite3.Row`) | ~14 |
| T17 | **DDG results loop ≈ `collect_results`** | `providers_html.py:45-80` vs `providers.py:137-165` | Shared accumulate-dedupe-clip helper fed by `(href, title, snippet)` triples | ~15 |
| T18 | **Scope-filter line; robots-gather twin (the one missing the #343 chunk deadline); compile-scope twin (256 spelled 3×)** | `admission_links.py:365`/`admission.py:137`; `admission_links.py:280-289`/`:309-317`; `admission_links.py:261`/`crawl_policy.py:145` | `_in_scope(pattern, url)` in `scope_match`; derive the boolean gatherer from `gather_robots_decisions` (fixes the missing chunk deadline too); one `ensure_scope_pattern()` in `safe_regex` | ~20 |
| T19 | **UA strings spelled twice each; `_async_sleep` ×3; `_looks_like_pdf` wrapper; `timeouts.for_request` restating defaults; `_quote`≡`_quote_identifier`; `origin_allowed` re-deriving loopback** | `request_profile.py`, `browser.py`, `browser_driver.py`, `timeouts.py`, `ddl.py`/`manifest.py`, `crawlutron/mcp/server.py`/`address_class.py` | Named constants + direct reuse | ~18 |

**Sum: ~350–420 LOC** — plus the drift-proofing value, which is the real prize: T4 and T5 each correspond to a live defect found in §2/§3.

### 4.3 The `reset_process_state` naming family (convention, not a defect)

`reset_process_state`/`has_process_state` pairs are defined in ~10 modules (async runner, engine, fetch_helpers, cookie jar, browser cookies, pinned transport, pooled clients, pooled cffi, content cache…). It's a deliberate test-isolation convention and each body is module-specific — **don't merge the bodies**. But the import-shadowing hazard is real (each module re-exports the same names into shared namespaces). If you touch this area: a tiny registry (`crawlutron/runtime/process_state.py` already exists for lifecycle) with `register(reset, has)` hooks; modules register instead of re-export. ~20 LOC net, mostly naming clarity.

### 4.4 Verified-NOT-duplicates (don't waste a refactor)

- `cache_control.py` vs `http_cache.py` — the latter *imports* the former; one parser.
- `cache.py` vs `content_cache.py` — validators-metadata vs body-envelope layers.
- `body_decode.py` vs `decompression.py` — decode vs decompress, disjoint.
- `orchestration.robots` vs `admission.RobotsGate` — the gate *wraps* the module; one matcher, one parser.
- `_link_values` (admission) vs sitemap `<loc>` collection — different layers (record shapes vs XML).
- `worker.py`'s two "redundant" TypeGuards — the comment is right; negation doesn't narrow for pyright.
- The four `0001_*` migration stubs — irreducible: yoyo's ledger requires one file per migration id.

### 4.5 Total LOC picture

| Bucket | LOC |
|---|---|
| Dead code (4.1) | ~300–330 |
| Structural consolidation (4.2) | ~350–420 |
| **Directly removable, zero wire change** | **~650–750 (~2.5–3% of app/)** |
| Secondary wins folded into fixes (§3.5 webhook-validation deletion, §3.9 settings plumbing, §3.14 in-place `known`, §2.7 render ladder) | ~40–60 |

Given the codebase is already jscpd-gated and aggressively funneled, this is the honest ceiling without behavior change. The remaining LOC arguments are *design* arguments: ~80 model modules could be grouped into ~10 cohesive files (pure moves, zero LOC change but a large navigation win), and the TypedDict↔BaseModel triads (T3) are the one place where a pattern change (build the model directly) both deletes code and closes a drift class.

---

## 5. Secrets Scan & Open-Source Readiness

### 5.1 Secrets scan — clean

Every module was swept for hardcoded credentials, tokens, internal hostnames, developer-machine paths, and personal data:

- **Zero hardcoded secrets** across all 262 modules. Search-provider keys (`tavily/brave/serper`) and `key_admin_token` are env-sourced with `""` defaults (`crawlutron/config/settings.py:100-108`); no sample-looking real keys anywhere; webhook signing secret is env-gated.
- **No internal URLs/hosts** beyond the intentional loopback default and the `host.docker.internal` denylist entries in `host_policy`. No developer names/emails in code. No debug prints or breakpoints (the migrations CLI's `print` is intended stdout).
- **Key material flows** verified: caller keys are returned once and stored only as SHA-256; keys never appear in logs, errors, the 202 body, or webhook payloads; pydantic validation errors are stripped to `loc`/`msg` (the offending `input` is dropped, `crawlutron/api/exceptions.py:50-66`).
- **Two latent leak channels to close before release** (both P2, both one-liners): `Credential.token/username/password` are repr-enabled (§2.11), and the four secret fields on `CrawlerSettings` lack `repr=False` (§2.11). Also: raw webhook URLs (which routinely embed receiver tokens) are logged verbatim (§2.12), and `env.py` config warnings log raw env values (`crawlutron/config/env.py:28,100,107` — truncate to ~12 chars).
- **Cookie payloads and `webhook_url` are stored plaintext in SQLite** with default file permissions — standard mitigation is operator disk security; say so in SECURITY.md.

### 5.2 OSS readiness checklist

| Item | State | Action |
|---|---|---|
| LICENSE file | **Missing** — `crawlutron/main.py` OpenAPI claims `license_info={"name": "MIT License"}` and a comment references "NOTICE carries the MIT lines", but no license text is in the tree | **Blocker.** Add LICENSE (MIT) + NOTICE before publishing; keep the OpenAPI claim consistent |
| SECURITY.md | Present and genuinely good (threat model, reporting SLA, operator guidance, `CRAWLUTRON_ALLOW_PRIVATE_TARGETS` warning discipline) | Update after §2/§3 fixes (webhook signature scheme change, anonymous-bucket caveat) |
| README / CONTRIBUTING / CHANGELOG / CODE_OF_CONDUCT | Present | CONTRIBUTING's docstrings-references and module tour still describe the *old* `crawlutron/crawl/*` layout in places (`docs/review-and-refactoring-plan.md` is archived-historical but reachable); refresh the tour to `crawlutron/{admission,api,orchestration,transport,…}` |
| Python floor | Code uses PEP 695 `type` aliases and generics ⇒ **requires ≥3.12**, but packaging isn't in the upload | Verify `requires-python = ">=3.12"` in pyproject; CI on 3.12+ |
| `regex` dependency | `crawlutron/safety/safe_regex.py` imports `regex` (hard, import-time, on the admission path) but the dependency isn't visible in the upload's metadata | Declare it; also `publicsuffix2`/equivalent if you adopt §2.3's PSL check |
| POSIX-only surfaces | `fcntl` (atomic_write, content_cache), cgroup readers, `os.sysconf` | Document "Linux container assumed" or lazy-import `fcntl`; on Windows the app currently fails at import |
| Repository identity | `crawlutron/vocabulary/identity.py:20` → `https://github.com/adambarber/crawlutron`, wired into `DEFAULT_USER_AGENT` and asserted against pyproject | Confirm the canonical public URL before release — this goes out on the wire in every request the crawler makes |
| Internal issue refs | Comments throughout: `#159`, `#199`, `#222`, `#241`, `#343`, `#381`, `#383`, `#387`, `#393-#414`, `#234`… | Convert to permanent links or plain prose; they're meaningless to the public |
| Orphaned comment fragments | ~30 mid-sentence comment stumps from refactors, e.g. `crawlutron/mcp/server.py:169`, `crawlutron/models/crawl_request.py:13`, `crawlutron/models/http_fetch_result.py:13`, `webhooks.py:53`, `runner.py:124`, `crawlutron/mcp/constants.py:6` | One sweep; each reads as file corruption to a new contributor |
| Typing | Nearly complete; ~15 implicit-`Any` seams (identity_router handler params via `_depends_singletons`, `scope_allows(compiled)` untyped in the *security* primitive, `safe_regex.match_bounded`'s `compiled`, `main.py` builders, watch_sweep `key_store=None`) | Fix the safety-primitive ones first (`safe_regex`, `scope_match`); annotate the rest opportunistically |
| CI/workflows | Not visible in the upload | If missing, add: ruff + strict-mypy + fast pytest suite + jscpd budget gate (the repo clearly already runs one) |
| Docs drift | `docs/` references `crawlutron/crawl/strategies/*` paths that no longer exist; `docs/reviews/ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md` documents a pre-refactor topology with old P0s (gzip double-decompress, cache-key collision, browser recycle — all verified **fixed** in this tree) | Mark clearly as historical (the refactoring-plan doc already is); point readers at this report's §2.15/§3.15 for current status |
| Naming | `SPILL_PREFIX = "bc-spill-"` leaks an ancestor project name into shared /tmp; `resource_pressure.py` is sizing-not-sensing; `map`/`search` handler names shadow builtins | Cheap renames during the LOC pass |

### 5.3 P3 register (consolidated — fix opportunistically)

**Admission/policy:** `int(float('inf'))` `OverflowError` → 500 in `validate_wire_policy` (`policy.py:226`, add `OverflowError`); `_code_for` equal-depth tie-break is lexicographic (`policy.py:117`); forwarding path of `checked_payload_urls` skips canonical dedupe (`payload_helpers.py:43`); `requeue_admitted_pages` never sets `known_after` (`admission.py:226`); `JobAdmissionContext.create` getattr-clamps are dead weight (`admission.py:85-92`); scope regex recompiled per job creation.
**API:** `_missing_sync_awaitable` unreachable helper (`crawl_endpoint.py:115`); `isinstance(webhook_url, str)` dead check (`:134`); `control_job` delete-race 500 (`jobs_router.py:185`); `main_helpers.request_gate` converts any engine `ValueError` into 422; `EnvelopeOptions.request_id` defaults `""` (make required); `settings or CrawlerSettings.from_mapping()` fallbacks ×3 hide DI breakage.
**Models:** `_strip_trailing_colon` path rewrites (`crawlutron/models/normalized_url.py:49`); query canonicalization reorders/re-encodes params (`:51-56` — document in OpenAPI); browser action `type` lowercased for validation but raw spelling stored (`crawl_policy_vocabulary.py:83` — confirm renderer matches case-insensitively); `crawlutron/mcp/server.py` unknown-tool is HTTP 200 + `-32602` while unknown-method is 404; `/docs` unauthenticated on non-loopback binds; `JobStatusResponse.owner` visibility is documented — keep the doc.
**Transport:** unknown URL schemes *continued* not aborted (`browser_targets.py:51`); screenshots unbounded into metadata (`browser_render_phases.py:185`); `__Host-`/`__Secure-` prefixes unenforced; `Path=` not normalized; https-subresource TOCTOU (documented residual — needs Playwright host-resolver override to close); WS `connect()` not awaited; no https→http downgrade block; custom caller headers survive cross-origin hops (document).
**Storage/search:** watch `create` doesn't dedupe (owner, url); `counts_by_job()` full-table correlated subquery per list; coordination `load` commits `touched_at` per read; `clear("")` can't target the anonymous bucket; 0003 `fetchall()` unbounded; `_ensure_wal` sync sleep loop.
**Runtime:** `is_warm()` lies under injected sensors (`pressure_gates.py:82`); `doc_worker_count` floors to 0 then clamps to 1 (use `ceil`); token-bucket `rate=0` unguarded at direct construction; metrics label interpolation unescaped; `logging_config` has no redaction denylist (one hostile `extra={"headers": …}` persists credentials — add a denylist at the formatter); silent log-level fallback.
**Extraction:** classification single-hit phrases misfire (`"not found"`, `"forbidden"`, `"security check"` → add to `SOFT_SINGLE_SIGNAL_CLASSIFICATIONS`); whitespace-only body classifies `rendered`; BOM handling; `parse_count` counts attempts; pagination regex vs `>` in attribute values; discovery js-endpoints/forms uncapped.

---

## 6. Remediation Roadmap

**Wave 1 — release blockers (P1, ~2–3 engineer-days, all verified with sketches above):**

| # | Fix | Section |
|---|-----|---------|
| 1 | `/v1/keys`: operator-token-only gate (or clamp-to-issuer + owner scoping) | §2.1 |
| 2 | Namespace the browser-cookie store by caller namespace (or make the header session-local) | §2.2 |
| 3 | Reject public-suffix / single-label `Domain=` cookies | §2.3 |
| 4 | Validate header *values* (CRLF/NUL) at the policy edge + credential model | §2.4 |
| 5 | Pin cffi via `resolve=` so impersonation works on HTTPS; log cffi handshake failures | §2.5 |
| 6 | Replace `text_from_html`'s regex with the linear `strip_html_noise` scanner | §2.6 |
| 7 | Bound render `timeout_seconds`/`max_retries` | §2.7 |
| 8 | Cap result-store payloads; batch/stream `payloads()`; move suggestions rollup off-loop | §2.8 + §3.7 |
| 9 | Bracket IPv6 in `NormalizedUrl` netloc + `origin` | §3.1 |
| 10 | Short failure-TTL + single-flight for the sitemap cache | §3.2 |
| 11 | Scheduler domain key: `site` everywhere (`make_admitted_task` one-liner + test) | §3.3 |
| 12 | `ProtocolValidationError` default message; `start_tracked_task` identity-checked callback; delete pre-loop webhook validation; recovery fails zombie PENDINGs | §3.4–§3.8 |

**Wave 2 — pre-freeze contract changes (do before tagging v1.0):** webhook HMAC over `{timestamp}.{body}` + treat 3xx as non-delivery (§2.13); stop logging raw webhook URLs; `repr=False` on credential + settings secrets (§2.11); LICENSE/NOTICE + repo URL confirmation (§5.2); `requires-python`/`regex` dependency verified.

**Wave 3 — the LOC program (mechanical, low-risk, one PR per bucket):** D1–D24 dead-code deletion (~330 LOC, run your suite between buckets); then T-buckets in drift-risk order: T5 (origin), T4 (taxonomy), T1 (owner fragment), T6 (scheduler factory), T3 (payload/response triads), T10 (terminal hop), T18 (robots gather + scope), then the rest.

**Wave 4 — hardening (P2/P3 register):** §3.9–§3.14 clusters, §5.3 register, and the two documented residuals you should *decide* on rather than leave implicit: browser https-subresource TOCTOU (needs host-resolver override) and zone-id hosts at admission (§2.9).

**Two standing practices worth keeping** (this review's meta-observation): the tree's habit of writing *why*-comments with issue numbers is why so much of it survived adversarial reading — keep it, just make the references public-permanent. And the jscpd budget gate is clearly working; after Wave 3, tighten the budget so the twins can't quietly regrow.

*Prior-review cross-check: the four P0s in `docs/reviews/ADVERSARIAL_REVIEW_BROWSER_CRAWLER.md` (gzip double-decompression, invalid-URL 422 handling, content-cache key collision, browser recycle on failure) were all re-verified as **fixed** in this tree — the cache key now JSON-encodes every part (`content_cache.py:108-110`), the recycle count is incremented in a `finally` that covers all attempts, and the content-cache key includes the full header set.*
