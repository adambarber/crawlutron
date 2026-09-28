# Browser-Crawler Telemetry Posture

**Invariant:** the browser-crawler service sends **zero** telemetry / usage
analytics / update phone-homes. This is a hard posture, not a preference: the
service crawls arbitrary public sites on a user's behalf and must not leak that
activity — or a stable device identifier — to third parties.

This document is the single home for *why* each guard exists and *what* is
intentionally left open, so a future reader (or a rescan) does not re-litigate
it or, worse, weaken it by accident.

## Where telemetry could enter

The runtime stack pulls in several third-party components that have, at various
versions, shipped client telemetry:

| Component | Role in the service | Telemetry behavior |
|---|---|---|
| `onnxruntime` (transitive via `magika`) | ML content-type inference during extraction | Windows-style OneCollector telemetry; device-ID persist at native-lib load |
| `playwright` (camoufox driver) | Drives the anti-detect browser for JS-heavy pages | Older versions sent 1DS usage stats; **removed** in this pinned version |
| `camoufox` | The browser itself + fingerprint data | Phone-homes **only on demand** (release/update check, IP/geo lookup) — never during a render |

## The two guard layers

### 1. Per-component opt-out (primary, where a flag exists)

`ORT_DISABLE_TELEMETRY=1` is baked into the runtime image
(`Dockerfile`, runtime stage `ENV`) and re-asserted in the compose
`environment:` block. onnxruntime reads it at native-lib load, so its
device-ID persist + telemetry init never run at all. This is the only component
in the stack that exposes a load-time kill switch.

### 2. Egress blackhole (un-bypassable chokepoint)

Because "the library's current version happens to not send" is a default we do
not want to trust across upgrades, the sinks themselves are made unreachable.
`docker-compose.yml` maps the Microsoft OneCollector / App-Insights host family
to `0.0.0.0` via `extra_hosts`:

- `mobile.events.data.microsoft.com`
- `browser.events.data.microsoft.com`
- `default.events.data.microsoft.com`
- `settings-win.data.microsoft.com`
- `dc.services.visualstudio.com`
- `rt.services.visualstudio.com`

`extra_hosts` writes `/etc/hosts`, which **every** resolver in the container
honours — glibc (`getaddrinfo`) for the Python fetchers **and** Chromium's
system resolver for browser navigations. A resolution to `0.0.0.0` fails the
connect before any bytes leave the process, so a library that ignores its own
flag still cannot reach its sink.

This chokepoint is placed at the deployment boundary, not at individual call
sites, so a new code path cannot route around it — consistent with the service's
general "harden the chokepoint, not the caller" rule.

## What is intentionally NOT blocked

These are **legitimate crawl egress** and must stay open — do not add them to
the blackhole:

- Any public site being crawled (the whole point of the service).
- `api.github.com` / GitHub / CDN hosts that a page genuinely references as a
  resource. (Camoufox's release/update check hits GitHub, but it is invoked only
  via an explicit operator action, never inside a crawl request, so it is not a
  per-crawl phone-home.)

The blackhole list is therefore a *bounded, reviewed* set of Microsoft analytics
endpoints, not a general egress filter.

## Escape hatch / future evolution

- **New ML or browser dependency added?** If it has its own telemetry flag, set
  it in the Dockerfile `ENV` block. If it does not, add its sink host(s) to the
  `extra_hosts` list. Either way the egress layer is the guarantee; the flag is
  a courtesy that also removes log noise.
- **Migrating to a dedicated repo:** this document, the Dockerfile `ENV` block,
  and the compose `extra_hosts` block are the complete, self-contained guard set
  — they lift together with the service tree.

## Verifying the posture (run after any rebuild)

```sh
# 1. onnxruntime must NOT emit the telemetry device-ID warning at import:
docker exec browser-crawler python -c 'import onnxruntime' 2>&1 | grep -c telemetry
#    -> expect 0

# 2. the OneCollector / App-Insights sinks must be sinkholed (0.0.0.0), not real IPs:
docker exec browser-crawler python -c 'import socket; \
  [print(h, socket.gethostbyname(h)) for h in \
  ["mobile.events.data.microsoft.com","dc.services.visualstudio.com"]]'
#    -> both resolve to 0.0.0.0
```
