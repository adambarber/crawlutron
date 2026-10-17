from __future__ import annotations

import asyncio
import logging
import os
import traceback
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.admission.policy import PolicyTier
from app.config.env import get_env_float, get_env_int

# Standalone JSON-line logging, wired once so every entry point logs the same;
# integrations override ``configure_service_logging`` in their own layer.
from app.models.address_class import is_loopback_bind
from app.models.host_policy import HostPolicy
from app.runtime.logging_config import configure_service_logging
from app.vocabulary.identity import SERVICE_ID, SERVICE_VERSION

from .config.settings import CrawlerSettings
from .storage.caller_key_store import CallerKeyStore
from .storage.job_store import JobStore
from .storage.result_store import CrawlResultStore
from .storage.watch_store import WatchStore
from .transport.browser_pool import (
    close_browser_pool,
    initialize_browser_pool,
)

if TYPE_CHECKING:
    # FastAPI is imported only for annotations under type checking; the runtime
    # import below carries the real classes (a bare variable is not a valid annotation).
    from fastapi import FastAPI, HTTPException
    from fastapi.exceptions import RequestValidationError
else:
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.exceptions import RequestValidationError
    except ImportError:
        # FastAPI is optional at import time (MCP/CLI/tests import this module
        # with no web server): FastAPI -> None, exceptions -> ``Exception``.
        FastAPI = None
        HTTPException = Exception
        RequestValidationError = Exception


# Import modular routers
from .discovery.agent_hints import create_agent_hints_router
from .discovery.capabilities import create_capabilities_router
from .discovery.well_known import create_well_known_router
from .health.readiness import create_health_router

settings = CrawlerSettings.from_mapping()

_TeardownStep = tuple[str, Callable[[], Awaitable[None]]]


async def _run_shutdown_steps(steps: Sequence[_TeardownStep]) -> None:
    """Run every teardown step, then report the ones that failed.

    Shutdown is where one failure must not decide what else is released, so each
    step runs whatever its predecessor did; failures raise together at the end.
    """
    failures: list[str] = []
    for name, step in steps:
        try:
            await step()
        except Exception:
            failures.append(f"{name}: {traceback.format_exc(limit=1).strip().splitlines()[-1]}")
    if failures:
        raise RuntimeError("shutdown steps failed: " + " | ".join(failures))


# ONE database: the store chokepoint opens a connection per schema and the
# runner filters by id; WAL serializes writers, so counters never contend.
DATABASE_PATH = settings.cache_dir + "/crawler.db"

# Job store for resumable crawls
job_store = JobStore(
    DATABASE_PATH,
    policy=HostPolicy.from_settings(settings),
    tier=PolicyTier.from_settings(settings),
)

# Completed single-request crawl results (fixes the dead status_url promise and
# makes results recoverable if the HTTP connection drops mid-response).
result_store = CrawlResultStore(
    DATABASE_PATH,
    ttl_seconds=get_env_float("CRAWLUTRON_RESULT_TTL_SECONDS", 3600.0),
    max_entries=get_env_int("CRAWLUTRON_RESULT_MAX_ENTRIES", 1000),
)

# Registered watches and revalidation history: no policy or tier -- a watch
# arrives admitted, and the sweep re-admits against the policy in force when it checks.
watch_store = WatchStore(DATABASE_PATH)

# Inbound caller keys: issuance, verification, and revocation for API-key identity.
caller_key_store = CallerKeyStore(DATABASE_PATH)

# Ensure the parent directory of the log file exists before the logger tries
# to open it; standalone checkouts default to ./logs which may not exist.
if settings.log_file_path:
    os.makedirs(os.path.dirname(settings.log_file_path) or ".", exist_ok=True)
logger = configure_service_logging(
    service_name=SERVICE_ID,
    log_level=settings.log_level,
    log_file_path=settings.log_file_path,
    log_file_max_bytes=settings.log_file_max_bytes,
    log_file_backup_count=settings.log_file_backup_count,
)
# Third-party extraction libraries log expected per-element WARNINGs (parser
# noise that cannot be acted on). Keep ERROR+ visible, drop the chatter.
logging.getLogger("trafilatura").setLevel(logging.ERROR)


def warn_if_bound_off_loopback(host: str) -> None:
    """Warn at startup when the API is reachable from off the machine."""
    if is_loopback_bind(host):
        return
    logger.warning(
        "Binding to %s exposes the crawl API on the network; callers without a "
        "key crawl anonymously (shared bucket, default policy limits), and "
        "issuance/revocation require CRAWLUTRON_KEY_ADMIN_TOKEN. Bind "
        "127.0.0.1, or keep this address and publish only to loopback as "
        "docker-compose.yml does.",
        (host or "").strip(),
    )


if FastAPI is not None:
    # Declared once so both branches agree on the type: the module-level app is
    # ``FastAPI | None`` by construction, not FastAPI that happens to be None.
    app: FastAPI | None

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # Startup: initialize the browser pool, then load the cookie jar.
        from .health.readiness import READINESS_STATE
        from .orchestration.engine import resource_gate

        # Readiness saturation comes from the live resource gate, not the
        # metrics gauges (which only recursive crawls refresh).
        READINESS_STATE.set_utilization_sampler(
            lambda: (resource_gate().memory_ratio(), resource_gate().cpu_ratio())
        )
        # The module-level gate is built at import time with no running
        # loop, so its lag probe must start here.
        resource_gate().start_lag_probe()

        # Before anything is listening: the exposure is a property of the bind,
        # true from the first moment, so stated before the first request.
        warn_if_bound_off_loopback(settings.host)

        # One browser pool only: initialized here, reached via get_browser_pool(),
        # with no second pool global.
        await initialize_browser_pool(settings)
        # Cookies first: recovered jobs can receive cookies in their first
        # batch, and a clear-then-populate load would drop those.
        if settings.cookie_persistence_enabled:
            # The jar is backed by the shared coordination store, not a JSON file
            # rewritten at shutdown (last-writer-wins); a legacy jar is imported once.
            os.makedirs(settings.cache_dir, exist_ok=True)
            from app.storage.coordination_cookies import SharedCookieStore
            from app.transport import cookie_jar as cookie_jar_module
            from app.transport.cookie_jar import CookieJar

            jar = CookieJar(SharedCookieStore(DATABASE_PATH))
            jar.import_file_into(settings.cookie_jar_path)
            cookie_jar_module.set_global_cookie_jar(jar)
            # Published like ``shutdown_event`` below: what the process is using
            # is otherwise answerable only via the module global the lifespan assigned.
            _app.state.cookie_jar = jar
        # /metrics reports the service, not this worker: counters go to the shared
        # store; gauges stay process-local (a shared cgroup summed reports nothing real).
        from app.storage.coordination_counters import CoordinationCounters

        from .runtime.metrics import get_metrics

        get_metrics().bind_counters(CoordinationCounters(DATABASE_PATH))

        # Jobs: rewind crash orphans and resume every PENDING whole-site job.
        from .api.job_runner import recover_and_start_pending_jobs

        # Minted in the lifespan, not at import: an import-time event is set by
        # the first teardown and cancels every later run's crawls.
        shutdown_event = asyncio.Event()
        _app.state.shutdown_event = shutdown_event
        await recover_and_start_pending_jobs(job_store, settings, shutdown_event)

        # Watches: the due-registry sweep, owned by the store's process (like job
        # recovery) and started before the first request so this deployment keeps them.
        from .api.watch_sweep import sweep_watches_forever

        watch_sweep_task = asyncio.create_task(
            sweep_watches_forever(
                watch_store,
                host_policy=HostPolicy.from_settings(settings),
                settings=settings,
                shutdown_event=shutdown_event,
            )
        )

        yield

        # Shutdown stops the world FIRST: no webhooks against cleared dependencies,
        # no cookies after the snapshot. The jar needs no step -- the store persists.
        shutdown_event.set()
        from .api.job_runner import shutdown_job_runner

        await shutdown_job_runner()
        # The sweep is cancelled, not awaited: cancellation leaves the watch due
        # (the at-least-once contract), and awaiting would web-hook against closing pools.
        watch_sweep_task.cancel()
        try:
            await watch_sweep_task
        except asyncio.CancelledError:
            pass

        from app.transport.http_client_pool import close_async_http_client, close_cffi_session

        # Imported here, not at module scope: this pulls in the markitdown stack,
        # which CLI/MCP callers that never render a PDF must not pay for.
        from .extraction.content import shutdown_doc_extraction_executor
        from .orchestration.engine import resource_gate

        async def _close_gate() -> None:
            resource_gate().close()

        # Every step is attempted and failures surface only after the last: a
        # close() that raises partway leaks every pool below it (and its subprocesses).
        await _run_shutdown_steps(
            (
                ("close_async_http_client", close_async_http_client),
                ("close_cffi_session", close_cffi_session),
                ("close_browser_pool", close_browser_pool),
                # The lazy document-extraction workers are the one pool nothing
                # above touches; without this their subprocesses outlive shutdown.
                ("shutdown_doc_extraction_executor", shutdown_doc_extraction_executor),
                ("resource_gate.close", _close_gate),
            )
        )
        # Symmetry with set_utilization_sampler(...): release the closed
        # gate's ratio sampler so nothing dereferences it post-shutdown.
        READINESS_STATE.set_utilization_sampler(None)

    app = FastAPI(
        title="Crawlutron Execution Service",
        lifespan=_lifespan,
        description=(
            "Localhost-only policy-driven crawler execution service for municipal script tools. "
            "GET /health is lightweight liveness for Docker healthchecks; GET /ready reports active crawl "
            "saturation without implying process death. "
            "Blocking direct HTTP, document extraction, Trafilatura, "
            "and synchronous browser work run off the FastAPI "
            "event loop so liveness stays responsive during crawls. "
            "Clients submit resolved crawl policies, inspect per-URL provenance, and use attempt history "
            "to make reviewable policy suggestions rather than mutating host policy automatically. "
            "The service classifies challenge/login/paywall "
            "outcomes but does not bypass, solve, "
            "outsource, rotate identities, rotate proxies, or inject credentials."
        ),
        version=SERVICE_VERSION,
        contact={"name": SERVICE_ID},
        license_info={"name": "MIT License"},
        openapi_tags=[
            {
                "name": "Health",
                "description": "Lightweight liveness and crawl-capacity readiness endpoints for local orchestration.",  # noqa: E501
            },
            {
                "name": "Discovery",
                "description": "Machine-readable service capabilities and AI agent guidance.",
            },
            {
                "name": "Crawler",
                "description": "Browser render and universal policy-driven crawl execution endpoints.",
            },
            {
                "name": "Search",
                "description": "Web search front-end discovery via the configured provider.",
            },
            {
                "name": "MCP",
                "description": "Model Context Protocol 2026-07-28 endpoint exposing crawler tools.",
            },
        ],
    )
    # Stash shared dependencies on ``app.state`` so router handlers reach them
    # via the :func:`get_settings` / :func:`get_shutdown_event` dependencies.
    app.state.crawler_settings = settings
    app.state.job_store = job_store
    app.state.result_store = result_store
    app.state.watch_store = watch_store
    app.state.caller_key_store = caller_key_store
    app.state.key_admin_token = settings.key_admin_token

    # Body-size guard: 16 MiB cap at the ASGI receive layer (see
    # app/api/middleware.py for how truncation and keep-alive are handled).
    from app.api.exceptions import http_exception_handler, validation_exception_handler
    from app.api.middleware import limit_request_body

    app.middleware("http")(limit_request_body)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)

    # Include modular routers
    health_router, _ = create_health_router()
    app.include_router(health_router)

    app.include_router(create_capabilities_router())
    app.include_router(create_agent_hints_router())
    app.include_router(create_well_known_router())
    from .api.crawl_router import router as crawl_router
    from .api.jobs_router import router as jobs_router
    from .api.keys_router import router as keys_router
    from .api.map_router import router as map_router
    from .api.metrics_router import router as metrics_router
    from .api.policy_router import router as policy_router
    from .api.render_router import router as render_router
    from .api.watch_router import router as watch_router
    from .mcp.router import router as mcp_router
    from .search.router import router as search_router

    app.include_router(metrics_router)
    app.include_router(render_router)
    app.include_router(map_router)
    app.include_router(jobs_router)
    app.include_router(keys_router)
    app.include_router(watch_router)
    app.include_router(crawl_router)
    app.include_router(policy_router)
    app.include_router(search_router)
    app.include_router(mcp_router)

else:
    # ``FastAPI`` is None here (the optional import failed): no app to build,
    # and the module-level ``app`` is None on this path -- readers must handle that.
    app = None
