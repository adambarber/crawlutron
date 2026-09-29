# Browser Crawler Event Loop Isolation and Readiness

**Status:** completed
**Created:** 2026-06-27
**Started:** 2026-06-27
**Completed:** 2026-06-27
**Branch:** current branch
**Related:** -

## Goal

Prevent long crawlutron requests from starving service liveness checks by moving blocking crawl work off the API event loop and exposing separate liveness and readiness endpoints.

## Scope

- Keep [`/health`](../../../../../docker/browser-crawler/app/main.py:234) as a lightweight liveness endpoint that should stay responsive even during active crawls.
- Add a readiness endpoint, likely `/ready`, that reports whether the crawler can accept work without causing Docker or OrbStack to treat active saturation as process death.
- Offload synchronous direct HTTP, document extraction, trafilatura extraction, and browser rendering paths invoked by [`crawl_request()`](../../../../../docker/browser-crawler/app/service.py:1126) so they do not block the FastAPI event loop.
- Add tests proving liveness remains responsive while crawl work is running and readiness exposes saturation metadata.
- Update Compose healthcheck behavior in [`docker-compose.municipal.yml`](../../../../../docker-compose.municipal.yml:51) only if needed to keep Docker health tied to liveness rather than readiness.

## Non-goals

- Do not redesign the crawler into a background job queue.
- Do not change caller-owned frontier expansion semantics.
- Do not add CAPTCHA solving, proxy rotation, identity rotation, or credential handling.
- Do not broaden Compose profiles or recreate unrelated LiteLLM services.

## Architecture Approach

### Event-loop isolation

The current request path enters [`crawl_request()`](../../../../../docker/browser-crawler/app/service.py:1126), schedules jobs with [`DomainScheduler.run()`](../../../../../docker/browser-crawler/app/scheduler.py:19), and then invokes synchronous strategy work. Direct HTTP currently uses [`urllib.request.urlopen()`](../../../../../docker/browser-crawler/app/service.py:117), and browser rendering runs through [`BrowserRenderer.render()`](../../../../../docker/browser-crawler/app/browser.py:55). These operations should run in worker threads when called from the async scheduler.

Implementation should prefer a minimal additive change:

- Wrap synchronous HTTP strategy execution around [`_http_strategy_result()`](../../../../../docker/browser-crawler/app/service.py:807) with thread offloading.
- Wrap synchronous browser strategy execution around [`_browser_strategy_result()`](../../../../../docker/browser-crawler/app/service.py:968) or its inner blocking render call with thread offloading.
- Keep result schemas unchanged.
- Preserve per-domain scheduling and fallback ordering in [`_execute_crawl_chain()`](../../../../../docker/browser-crawler/app/service.py:1029).

### Liveness and readiness split

- Keep [`/health`](../../../../../docker/browser-crawler/app/main.py:234) returning only process liveness.
- Add readiness state that tracks active crawl count, configured global capacity, and whether the service is saturated.
- Add a new readiness endpoint in [`docker/browser-crawler/app/main.py`](../../../../../docker/browser-crawler/app/main.py:234), likely `/ready`, returning HTTP 200 when below saturation and HTTP 503 when saturated or shutting down.
- Keep Docker healthcheck in [`docker-compose.municipal.yml`](../../../../../docker-compose.municipal.yml:51) pointed at [`/health`](../../../../../docker/browser-crawler/app/main.py:234), not readiness, so active saturation does not mark the container unhealthy.

## Files Likely to Change

- [`docker/browser-crawler/app/service.py`](../../../../../docker/browser-crawler/app/service.py:807)
- [`docker/browser-crawler/app/main.py`](../../../../../docker/browser-crawler/app/main.py:234)
- [`docker/browser-crawler/app/settings.py`](../../../../../docker/browser-crawler/app/settings.py:12)
- [`docker/browser-crawler/app/api_models.py`](../../../../../docker/browser-crawler/app/api_models.py:154), if adding typed readiness response models
- [`docker-compose.municipal.yml`](../../../../../docker-compose.municipal.yml:51), if healthcheck endpoint or readiness env settings need documenting in Compose
- [`tests/test_crawlutron_api.py`](../../../../../tests/test_crawlutron_api.py:24)
- [`tests/test_crawlutron_core.py`](../../../../../tests/test_crawlutron_core.py:22)
- [`README.md`](../../../../../README.md:283) or [`docs/references/crawlutron-agent-contract.md`](../../../../../docs/references/crawlutron-agent-contract.md:9), if the readiness contract should be durable documentation

## Tasks

- [ ] Write a failing API test in [`tests/test_crawlutron_api.py`](../../../../../tests/test_crawlutron_api.py:24) for `/ready` returning readiness JSON with active crawl count, configured capacity, and saturated status.
- [ ] Write a failing API test in [`tests/test_crawlutron_api.py`](../../../../../tests/test_crawlutron_api.py:24) that confirms [`/health`](../../../../../docker/browser-crawler/app/main.py:234) remains a simple liveness response and is the endpoint used by Compose healthchecks.
- [ ] Write a failing core test in [`tests/test_crawlutron_core.py`](../../../../../tests/test_crawlutron_core.py:22) that proves blocking direct HTTP strategy work is offloaded and does not block the event loop.
- [ ] Write a failing core test in [`tests/test_crawlutron_core.py`](../../../../../tests/test_crawlutron_core.py:22) that proves blocking browser strategy work is offloaded and does not block the event loop.
- [ ] Run `python3 -m unittest tests.test_crawlutron_api tests.test_crawlutron_core -v` and confirm the new tests fail for the expected reasons.
- [ ] Implement a small readiness state tracker, preferably in [`docker/browser-crawler/app/main.py`](../../../../../docker/browser-crawler/app/main.py:234) or a focused new module under [`docker/browser-crawler/app/`](../../../../../docker/browser-crawler/app/), with active count and saturation calculation.
- [ ] Wire crawl and render endpoints to increment/decrement readiness active counters around request execution.
- [ ] Implement thread offloading in [`docker/browser-crawler/app/service.py`](../../../../../docker/browser-crawler/app/service.py:807) for direct HTTP/document/trafilatura strategy execution.
- [ ] Implement thread offloading for browser rendering invoked from [`_browser_strategy_result()`](../../../../../docker/browser-crawler/app/service.py:968) while preserving existing result shape.
- [ ] Add `/ready` OpenAPI metadata and response model if using [`api_models.py`](../../../../../docker/browser-crawler/app/api_models.py:154).
- [ ] Verify [`docker-compose.municipal.yml`](../../../../../docker-compose.municipal.yml:51) continues to use [`/health`](../../../../../docker/browser-crawler/app/main.py:234) for Docker healthcheck.
- [ ] Run `python3 -m unittest tests.test_crawlutron_api tests.test_crawlutron_core -v` and confirm targeted tests pass.
- [ ] Run `./test.sh crawlutron` for the repository crawlutron verification group.
- [ ] Run `docker compose --profile crawlutron config crawlutron` to validate Compose shape.
- [ ] Rebuild and restart the service with [`scripts/crawlutron.sh rebuild`](../../../../../scripts/crawlutron.sh:161) after code changes.
- [ ] Verify local liveness with [`scripts/crawlutron.sh check`](../../../../../scripts/crawlutron.sh:173).
- [ ] Manually exercise a Worcester crawl and confirm [`/health`](../../../../../docker/browser-crawler/app/main.py:234) stays responsive during long work and `/ready` reports active/saturated state accurately.

## Verification Commands

```text
python3 -m unittest tests.test_crawlutron_api tests.test_crawlutron_core -v
python3 -m unittest tests.test_crawlutron_api tests.test_config_validation -v
./test.sh crawlutron
docker compose --profile crawlutron config crawlutron
./scripts/crawlutron.sh rebuild
./scripts/crawlutron.sh check
curl -sS http://127.0.0.1:${CRAWLUTRON_HOST_PORT:-8765}/ready
```

## Verification Recorded

```text
python3 -m unittest tests.test_crawlutron_api tests.test_crawlutron_core -v
./test.sh crawlutron
bash -lc 'source ./scripts/lib/dev-stack-lifecycle.sh; args=(); dev_stack_compose_file_args args; docker compose "${args[@]}" --profile crawlutron config crawlutron >/tmp/dev-stack-crawlutron-config.txt && echo OK'
python3 -m unittest tests.test_crawlutron_api tests.test_config_validation -v
./test.sh crawlutron
./scripts/crawlutron.sh rebuild
./scripts/crawlutron.sh check
curl /ready, /v1/capabilities, and /v1/agent-hints against http://127.0.0.1:${CRAWLUTRON_HOST_PORT:-8765}
```

## Rollback and Safe-stop Notes

- If thread offloading changes behavior unexpectedly, revert [`docker/browser-crawler/app/service.py`](../../../../../docker/browser-crawler/app/service.py:807) first and keep the readiness endpoint change isolated if tests pass.
- If Docker health becomes unstable, ensure [`docker-compose.municipal.yml`](../../../../../docker-compose.municipal.yml:51) healthcheck still calls [`/health`](../../../../../docker/browser-crawler/app/main.py:234), not `/ready`.
- Restart only crawlutron with [`scripts/crawlutron.sh restart`](../../../../../scripts/crawlutron.sh:170); do not use broad Compose startup commands that recreate unrelated services.

## Completion Checklist

- [x] Implementation done
- [x] Verification recorded
- [ ] Reusable decisions copied to [`docs/decisions/`](../../../../../docs/decisions/)
- [x] Reusable procedures copied to [`docs/runbooks/`](../../../../../docs/runbooks/)
- [x] Reusable references copied to [`docs/references/`](../../../../../docs/references/)
- [x] Work moved to [`docs/work/complete/YYYY/MM/`](../../../../../docs/work/complete/)
- [ ] Related GitHub issue updated or closed with `gh`, if applicable
