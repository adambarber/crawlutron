# SSRF architecture

crawlutron treats every egress target as untrusted until it is classified,
resolved fresh, pinned to its IP, and re-validated at each hop. The guard is
fail-closed: any private, loopback, link-local, or otherwise non-public
address is refused before a connection is made, and the decision is revisited
on every redirect and every browser subresource. This page documents the
classification primitive, the per-fetch pin, redirect re-anchoring, the
browser Host rewrite, the MCP Origin check, and the private-target escape
hatch — each with the file:line or test that proves it.

## The classification primitive (address_class.py)

Every address is parsed once, unwrapped once, and read against a range in
`packages/safe-url/src/safe_url/address_class.py:134` (`classify`). The verdicts are
`PUBLIC`, `PRIVATE`, `LOOPBACK`, `LINK_LOCAL`, `CGNAT` (`100.64.0.0/10`),
`UNIQUE_LOCAL` (`fc00::/7`), `NAT64` (`64:ff9b::/96` and the RFC 8215
`64:ff9b:1::/48`), `MULTICAST`, `UNSPECIFIED`, `RESERVED`, and
`NOT_AN_ADDRESS` (`address_class.py:28-44`). `NOT_AN_ADDRESS` is a first-class
answer, not a failure: folding "could not parse" into `PUBLIC` would silently
invert every consumer's decision (`address_class.py:30-31`).

Three wrapper/escaping forms are unwrapped before the range check so a
wrapper cannot hide what the range check rejects
(`address_class.py:102-113`):

- **IPv4-mapped IPv6** — `addr.ipv4_mapped`
- **6to4** — `addr.sixtofour`
- **Teredo** — `addr.teredo[1]`; the client is the reachable target

Resolver-numeric forms `ipaddress` rejects (bare integers, hex `0x`, octal
`0`, short dotted quads) are interpreted the way glibc-style resolvers do
(`address_class.py:47-99`), and the packed value is classified itself, so
`0`, `0x7f000001`, `0177`, and `127.1` cannot slip through as "not an
address". First match wins and `PUBLIC` is the fallback
(`address_class.py:134-164`), so private/reserved are both "not PUBLIC".

Proof: `tests/transport/test_dns_cache.py:55-66` asserts `127.0.0.1` is
refused with "resolves to private/loopback IP"; `tests/test_ssrf_route_subresources.py`
covers route-level interception and hostname pinning.

## Per-fetch resolve + pin (pinned_dns.py, pinned_transport.py)

`check_ssrf_protection` (`pinned_dns.py:179`) resolves the host and aborts if
any IP is private/loopback; it is skipped only when `allow_private_targets()`
is true (`pinned_dns.py:184`, `host_policy.py:103`). The validation path
reuses a short-TTL cache (`_DNS_TTL = 60.0`, `pinned_dns.py:69`); the
pinning path deliberately does **not** (`_resolve_ips` with `use_cache=False`,
`pinned_dns.py:174`, `:218`) so a rebind inside the TTL cannot bypass SSRF —
a cached IP never bypasses the guard through a TOCTOU
(`pinned_dns.py:134`).

`resolve_and_pin_host` (`pinned_dns.py:206`) resolves the host **once**,
pins the resulting IP, and connects to that exact IP to defeat DNS-rebinding
TOCTOU. It fails closed on every OS resolver fault
(`pinned_dns.py:219-222`) and refuses a mixed public/private answer set
(`pinned_dns.py:225-229`), matching the browser ruling. `build_pinned_request`
(`pinned_transport.py:54`) composes the connect URL to the pinned IP while
preserving the logical hostname as `Host` and TLS SNI — one resolution per
hop, and identity travels with the pin (`pinned_transport.py:41-43`, #574).

Proof: `tests/transport/test_dns_cache.py:30-52` shows the validation check is
cached within the TTL (`test_ssrf_check_caches_within_ttl`) while the pin
path resolves fresh each call
(`test_resolve_pin_host_resolves_fresh_each_call`, `calls["n"] == 2`).

## Redirect re-anchor + origin_of rule (pinned_transport.py, credentials.py)

Redirects are followed by hand, one hop at a time
(`pinned_transport.py:214` `RedirectChain`,
`pinned_transport.py:172` `redirect_target`), never left to the HTTP client's
automatic redirect, so each hop is re-pinned and re-checked.
`MAX_REDIRECT_HOPS = 5` (`pinned_transport.py:163`); hop exhaustion raises
rather than reading the redirect body as page content
(`pinned_transport.py:184-188`).

On a cross-origin hop, caller credentials are stripped before the hop
(`pinned_transport.py:201` `_stripped_credentials`,
`_CREDENTIAL_HEADERS` `pinned_transport.py:169` = `{cookie, authorization,
proxy-authorization}`); within one origin they are kept
(`tests/transport/test_redirect_target.py:90-100`). The boundary is the
**origin** (scheme+host+port), not the host — a port change or scheme
downgrade scrubs credentials
(`tests/transport/test_redirect_target.py:103-129`). The canonical origin
spelling is `origin_of` in `packages/core/src/crawlutron/admission/credentials.py:19-25`,
which raises on non-HTTP schemes, userinfo, and any host admission refuses,
and `apply_credentials` (`credentials.py:51-68`) attaches `Authorization`
iff the URL's origin holds a credential, by exact canonical-origin equality:
a credential for origin A can never attach to origin B.

Proof: `tests/transport/test_redirect_target.py:78-85` (cross-host credential
stripping) and `:90-100` (same-origin credentials kept).

## Browser Host-rewrite pin (browser_targets.py)

Browser navigation has no dial ownership: the browser performs its own DNS
and connect after the route handler's validation, so the last verifiable
moment is the handler itself (`tests/transport/test_browser_target_stability.py:1-10`).

`resolve_browser_target` (`browser_targets.py:40`) gate-keeps HTTPS with the
fresh+stable double resolve `check_ssrf_protection_stable` (#617) and keeps
the hostname for TLS SNI/cert verification; plain HTTP pins the resolved IP
with the original `Host`. `make_ssrf_route_handler` (`browser_targets.py:70`)
is the Playwright `page.route` handler that aborts any navigation, redirect,
or subresource to an SSRF-unsafe host, re-pinning per subresource request and
failing closed on any exception (`browser_targets.py:130-159`). `Host`
header overrides are applied per hop; empty overrides are omitted so
Playwright does not wipe UA/cookies/accept
(`browser_targets.py:117-125`). WebSocket upgrades are handled by
`make_ssrf_websocket_handler` (`browser_targets.py:162`), closing any that
resolve internal.

Proof: `tests/transport/test_browser_target_stability.py:22-72` — an HTTPS
subresource whose second fresh answer differs from the first is aborted
(`route.decision == "abort"`), identical stable answers pass, and answer
order is ignored (set comparison, not list comparison).

## MCP Origin check (middleware.py, mcp/server.py)

`reject_hostile_origin` (`packages/rest/src/crawlutron_rest/api/middleware.py:22-41`) refuses a
present, non-loopback browser `Origin` on every surface: `/mcp` answers with
the JSON-RPC error object, every REST route with the error envelope, and the
Origin is never echoed back (#655). `origin_allowed`
(`packages/mcp/src/crawlutron_mcp/server.py:42-56`) admits only an absent origin or a
loopback hostname (`localhost`, `127.0.0.1`, `::1`); any cross-origin browser
origin is rejected with 403. Callers authenticate by API key, so the Origin
guard is a DNS-rebinding defense layered on top of caller auth, not a
substitute for it.

Proof: `tests/api/test_origin_guard.py:9-43` — hostile Origin on a REST GET
is rejected `403` with the envelope `origin_not_allowed`, the Origin is not
echoed (#655), and absent/loopback Origins pass.

## The private-target escape hatch (host_policy.py)

`CRAWLUTRON_ALLOW_PRIVATE_TARGETS` (default unset/false) disables the SSRF
private-target guard: caller URLs may reach private, loopback, and
link-local addresses including cloud metadata endpoints
(`host_policy.py:85-160`). The decision is memoized per raw value and warns
once per distinct enabled value through the owner's logger
(`env.py:43-78`, `host_policy.py:88-100`):

> SSRF private-target guard DISABLED by
> CRAWLUTRON_ALLOW_PRIVATE_TARGETS=%r: caller-supplied URLs may now reach
> private, loopback, and link-local addresses, including cloud metadata
> endpoints. Unset it, or set it to 0, outside local development.

This is local-dev only. **When the private-target guard is enabled** all the
guarantees above apply; when it is disabled, private/loopback targets are
admitted and the SSRF boundary is intentionally relaxed.

## Guarantee → proof index

| Guarantee | Where it lives | Test proving it |
|---|---|---|
| Address classification incl. IPv4-mapped/6to4/Teredo, resolver-numeric forms | `packages/safe-url/src/safe_url/address_class.py:28-164` | `tests/test_ssrf_route_subresources.py`, `tests/transport/test_dns_cache.py` |
| Per-fetch fresh pin; validation cached, pin never cached | `packages/core/src/crawlutron/transport/pinned_dns.py:206`, `:174`, `:189` | `tests/transport/test_dns_cache.py:30-52` |
| Redirect re-anchor, hop budget `MAX_REDIRECT_HOPS = 5`, credential strip | `packages/core/src/crawlutron/transport/pinned_transport.py:172`, `:163`, `:201` | `tests/transport/test_redirect_target.py:61-142` |
| Browser Host-rewrite pin, stable-answer double resolve | `packages/core/src/crawlutron/transport/browser_targets.py:40`, `:70` | `tests/transport/test_browser_target_stability.py:22-72` |
| MCP Origin check, non-loopback → 403, Origin never echoed | `packages/rest/src/crawlutron_rest/api/middleware.py:22-41`, `packages/mcp/src/crawlutron_mcp/server.py:42-56` | `tests/api/test_origin_guard.py:9-43` |
| Private-target guard; warned once per enabled value at first use | `packages/safe-url/src/safe_url/host_policy.py:85-160` | `tests/transport/test_dns_cache.py:55-66` |
