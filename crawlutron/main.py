from __future__ import annotations

import asyncio
import logging
import os
import traceback
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from crawlutron.admission.policy import PolicyTier
from crawlutron.config.env import get_env_float, get_env_int
from crawlutron.models.address_class import is_loopback_bind
from crawlutron.models.host_policy import HostPolicy
from crawlutron.runtime.pressure_sizing import default_executor_workers
from crawlutron.vocabulary.identity import SERVICE_ID, SERVICE_VERSION

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
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
else:
    try:
        from fastapi import FastAPI
        from fastapi.exceptions import RequestValidationError
    except ImportError:
        # FastAPI is optional at import time (MCP/CLI/tests import this module
        # with no web server): FastAPI -> None, exceptions -> ``Exception``.
        FastAPI = None
        RequestValidationError = Exception


# Import modular routers
from .discovery.agent_hints import create_agent_hints_router
from .discovery.capabilities import create_capabilities_router
from .discovery.well_known import create_well_known_router
from .health.readiness import create_health_router
from .runtime.logging_config import configure_service_logging

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


def create_app(settings: CrawlerSettings | None = None) -> FastAPI:
    """Build the wired service app: stores, logging, lifespan, routers.

    The one factory for every entry point; importing this module stays
    side-effect free so CLI/MCP paths construct nothing they do not use.
    """
    if FastAPI is None:
        raise RuntimeError("create_app requires FastAPI, which is not importable here")
    settings = settings or CrawlerSettings.from_mapping()
    database_path = settings.cache_dir + "/crawler.db"
    job_store = JobStore(
        database_path,
        policy=HostPolicy.from_settings(settings),
        tier=PolicyTier.from_settings(settings),
    )
    result_store = CrawlResultStore(
        database_path,
        ttl_seconds=get_env_float("CRAWLUTRON_RESULT_TTL_SECONDS", 3600.0),
        max_entries=get_env_int("CRAWLUTRON_RESULT_MAX_ENTRIES", 1000),
    )
    watch_store = WatchStore(database_path)
    caller_key_store = CallerKeyStore(database_path)
    _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await _startup_services(app, settings, database_path)
        yield
        await _shutdown_services(app)

    return _build_app(
        settings,
        job_store=job_store,
        result_store=result_store,
        watch_store=watch_store,
        caller_key_store=caller_key_store,
        lifespan=lifespan,
    )


def _configure_logging(settings: CrawlerSettings) -> logging.Logger:
    """Wire the standalone JSON logging; opt-in via create_app, never at import."""
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
    return logger


def warn_if_bound_off_loopback(host: str) -> None:
    """Warn at startup when the API is reachable from off the machine."""
    if is_loopback_bind(host):
        return
    logging.getLogger(SERVICE_ID).warning(
        "Binding to %s exposes the crawl API on the network; callers without a "
        "key crawl anonymously (shared bucket, default policy limits), and "
        "issuance/revocation require CRAWLUTRON_KEY_ADMIN_TOKEN. Anonymous "
        "callers have no caller authentication. Bind 127.0.0.1, or keep this "
        "address and publish only to loopback as docker-compose.yml does.",
        (host or "").strip(),
    )


def _build_app(
    settings: CrawlerSettings,
    *,
    job_store,
    result_store,
    watch_store,
    caller_key_store,
    lifespan,
) -> FastAPI:
    """Construct the FastAPI app: state, middleware, handlers, and routers."""
    app = FastAPI(
        title="Crawlutron Execution Service",
        lifespan=lifespan,
        description=(
            "Localhost-only policy-driven crawler execution service for automated script callers. "
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
        openapi_tags=_openapi_tags(),
    )
    _stash_state(app, settings, job_store, result_store, watch_store, caller_key_store)
    _install_error_surface(app)
    _include_routers(app)
    return app


def _openapi_tags() -> list[dict[str, str]]:
    """The service's OpenAPI tag groups, in the order the docs render them."""
    return [
        {
            "name": "Health",
            "description": "Liveness and crawl-capacity readiness endpoints for local orchestration.",
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
    ]


def _stash_state(app, settings, job_store, result_store, watch_store, caller_key_store) -> None:
    """Stash shared dependencies on ``app.state`` for the router dependencies."""
    app.state.crawler_settings = settings
    app.state.job_store = job_store
    app.state.result_store = result_store
    app.state.watch_store = watch_store
    app.state.caller_key_store = caller_key_store
    app.state.key_admin_token = settings.key_admin_token
    # ONE per-process register of in-flight sync work (crawl/map/render/search)
    # so every sync door counts against the caller's one concurrency ceiling.
    from crawlutron.api.caller_concurrency import CallerConcurrencyTracker

    app.state.concurrency_tracker = CallerConcurrencyTracker()


def _install_error_surface(app) -> None:
    """Middleware and exception handlers every response path funnels through."""
    # Body-size guard: 16 MiB cap at the ASGI receive layer (see
    # crawlutron/api/middleware.py for how truncation and keep-alive are handled).
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from crawlutron.api.anonymous_throttle import throttle_anonymous_surfaces
    from crawlutron.api.exceptions import http_exception_handler, validation_exception_handler
    from crawlutron.api.middleware import limit_request_body, reject_hostile_origin

    app.middleware("http")(throttle_anonymous_surfaces)
    app.middleware("http")(reject_hostile_origin)
    app.middleware("http")(limit_request_body)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    # Framework-level 404/405 raise Starlette's base HTTPException (parent of
    # FastAPI's); register on the base + Exception for the last-resort 500.
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(Exception, http_exception_handler)


def _include_routers(app) -> None:
    """Mount every feature router, health first, MCP last."""
    health_router, _ = create_health_router()
    app.include_router(health_router)

    app.include_router(create_capabilities_router())
    app.include_router(create_agent_hints_router())
    app.include_router(create_well_known_router())
    from .api.router_registry import ROUTER_MODULES
    from .mcp.router import router as mcp_router

    for feature_router in ROUTER_MODULES:
        app.include_router(feature_router)
    app.include_router(mcp_router)


if FastAPI is not None:
    # Declared once so both branches agree on the type: the module-level app is
    # ``FastAPI | None`` by construction, not FastAPI that happens to be None.
    app: FastAPI | None

    _module_app: FastAPI | None = None

    def __getattr__(name: str):
        """Lazily build the ASGI app on first attribute access.

        Importing this module constructs nothing; ``app`` discovery (uvicorn,
        tests) triggers one create_app() cached for the process lifetime.
        """
        global _module_app
        if name == "app":
            if _module_app is None:
                _module_app = create_app()
            return _module_app
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

else:
    # ``FastAPI`` is None here (the optional import failed): no app to build,
    # and the module-level ``app`` is None on this path -- readers must handle that.
    app = None


def install_default_executor(loop: asyncio.AbstractEventLoop) -> None:
    """Size the loop's shared asyncio.to_thread executor once.

    Python's implicit default is min(32, cpu+4) with an unbounded queue; the
    explicit install makes the bound derived, named and operator-tunable.
    """
    from concurrent.futures import ThreadPoolExecutor

    try:
        executor = ThreadPoolExecutor(max_workers=default_executor_workers(), thread_name_prefix="asyncio")
        loop.set_default_executor(executor)
    except Exception:
        # Sizing must never take down startup: the loop falls back to its own
        # implicit default, the exact behavior this install exists to improve.
        logging.getLogger(__name__).warning("default_executor_install_failed", exc_info=True)


async def _startup_services(_app: FastAPI, settings: CrawlerSettings, database_path: str) -> None:
    """Start the readiness sampler, pools, cookies, metrics, jobs and watches.

    Startup order is load-bearing: the pool initializes before cookies load,
    cookies load before job recovery (a clear-then-populate load drops them).
    """
    install_default_executor(asyncio.get_running_loop())
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
    _load_cookie_jar(settings, _app, database_path)
    _bind_metrics(database_path)
    await _recover_jobs_and_start_sweep(_app, settings)


def _bind_metrics(database_path: str) -> None:
    """Bind the shared-store counters; gauges stay process-local.

    /metrics reports the service, not this worker: counters go to the shared
    store (a shared cgroup summed reports nothing real).
    """
    from crawlutron.storage.coordination_counters import CoordinationCounters

    from .runtime.metrics import get_metrics

    get_metrics().bind_counters(CoordinationCounters(database_path))


async def _recover_jobs_and_start_sweep(_app: FastAPI, settings: CrawlerSettings) -> None:
    """Rewind crash orphans, resume PENDING jobs, start the watch sweep.

    The shutdown event is minted in the lifespan, not at import: an import-time
    event is set by the first teardown and cancels every later run's crawls.
    """
    from .api.job_recovery import recover_and_start_pending_jobs
    from .api.job_runner import start_job
    from .api.watch_sweep import sweep_watches_forever

    shutdown_event = asyncio.Event()
    _app.state.shutdown_event = shutdown_event
    await recover_and_start_pending_jobs(_app.state.job_store, settings, shutdown_event, start_job)
    _app.state.watch_sweep_task = asyncio.create_task(
        sweep_watches_forever(
            _app.state.watch_store,
            host_policy=HostPolicy.from_settings(settings),
            settings=settings,
            shutdown_event=shutdown_event,
            key_store=_app.state.caller_key_store,
            concurrency_tracker=_app.state.concurrency_tracker,
            job_store=_app.state.job_store,
        )
    )


def _load_cookie_jar(settings: CrawlerSettings, _app: FastAPI, database_path: str) -> None:
    """Load the shared cookie jar when cookie persistence is on.

    Cookies load before job recovery (a clear-then-populate load drops them);
    the jar is backed by the shared coordination store, not a JSON rewrite.
    """
    if not settings.cookie_persistence_enabled:
        return
    os.makedirs(settings.cache_dir, exist_ok=True)
    from crawlutron.storage.coordination_cookies import SharedCookieStore
    from crawlutron.transport import cookie_jar as cookie_jar_module
    from crawlutron.transport.cookie_jar import CookieJar

    jar = CookieJar(SharedCookieStore(database_path))
    jar.import_file_into(settings.cookie_jar_path)
    cookie_jar_module.set_global_cookie_jar(jar)
    # Published like ``shutdown_event``: what the process is using
    # is otherwise answerable only via the module global the lifespan assigned.
    _app.state.cookie_jar = jar


async def _shutdown_services(_app: FastAPI) -> None:
    """Stop the world FIRST: no webhooks against cleared dependencies, no
    cookies after the snapshot. The jar needs no step -- the store persists."""
    from .health.readiness import READINESS_STATE
    from .orchestration.engine import resource_gate

    shutdown_event = _app.state.shutdown_event
    shutdown_event.set()
    await _drain_watch_sweep(_app)
    from .api.job_runner import shutdown_job_runner

    await shutdown_job_runner()

    from crawlutron.transport.http_client_pool import close_async_http_client, close_cffi_session

    # Imported here, not at module scope: this pulls in the markitdown stack,
    # which CLI/MCP callers that never render a PDF must not pay for.
    from .extraction.content import shutdown_doc_extraction_executor

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


async def _drain_watch_sweep(_app: FastAPI) -> None:
    """Cancel and await the watch sweep before the job runner drains.

    The sweep is cancelled while the key store is still open: a pass that
    consults it for liveness must not race a closed handle.
    """
    watch_sweep_task = getattr(_app.state, "watch_sweep_task", None)
    if watch_sweep_task is None:
        return
    watch_sweep_task.cancel()
    try:
        await watch_sweep_task
    except asyncio.CancelledError:
        pass
