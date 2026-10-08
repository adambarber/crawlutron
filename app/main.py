from __future__ import annotations

import asyncio
import logging
import os
import traceback
from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

# Standalone JSON-line logging configuration, wired once here so every
# entry point logs the same way. Integration deployments that want richer
# logging can override ``configure_service_logging`` in their own
# integration layer.
from app.addresses import is_loopback_bind
from app.crawl.policy import PolicyTier
from app.env import get_env_float, get_env_int
from app.logging_config import configure_service_logging
from app.models.host_policy import HostPolicy
from app.vocabulary.identity import SERVICE_ID, SERVICE_VERSION

from .crawl.job_store import JobStore
from .crawl.result_store import CrawlResultStore
from .crawl.watch_store import WatchStore
from .settings import CrawlerSettings
from .transport.browser_pool import (
    close_browser_pool,
    initialize_browser_pool,
)

if TYPE_CHECKING:
    # FastAPI is only imported for annotations under type checking; the runtime
    # import below carries the real classes, and this keeps the names usable as
    # types (a bare pre-declared variable is not a valid type annotation).
    from fastapi import FastAPI, HTTPException
    from fastapi.exceptions import RequestValidationError
else:
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.exceptions import RequestValidationError
    except ImportError:
        # FastAPI is optional at import time (the MCP/CLI surface and tests
        # import this module for its settings and pool wiring without a web
        # server). ``FastAPI`` becomes None so the ``if FastAPI is not None``
        # guard further down reads it; the two exception types become
        # ``Exception`` so handler registration below still types.
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

    Shutdown is the one place where a single failure must not be allowed to
    decide what else gets released: the pools below a raising ``close()`` are
    not retried by anything, and the last of them outlives the process
    entirely. Every step is therefore attempted regardless of what its
    predecessor did, and the failures are raised together once the last one has
    run -- so this widens the blast radius of a broken close without hiding it,
    which a bare ``try``/``except``/``continue`` would.
    """
    failures: list[str] = []
    for name, step in steps:
        try:
            await step()
        except Exception:
            failures.append(f"{name}: {traceback.format_exc(limit=1).strip().splitlines()[-1]}")
    if failures:
        raise RuntimeError("shutdown steps failed: " + " | ".join(failures))


# ONE database for the whole service. The store chokepoint opens a connection
# per StoreSchema and the migration runner filters each schema's migrations by
# id against the file's single ledger, so five schemas in one file cost nothing
# structurally -- and a second file would mean a second ledger, a second backup,
# and a second thing to reason about when something is locked.
#
# WAL serializes writers, so the small frequent counter and cookie writes do
# not contend for anything the large infrequent job writes need.
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

# Registered watches and their revalidation history. No policy or tier: a watch
# arrives admitted, and the sweep re-admits against the policy in force when it
# checks, so a row outlives the deployment that wrote it.
watch_store = WatchStore(DATABASE_PATH)

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
# The single browser pool lives in the browser_pool module (set up in lifespan
# via initialize_browser_pool); request handlers reach it through
# get_browser_pool(). There is intentionally no second pool global here.
#
# The shutdown event is minted inside the lifespan and published to
# ``app.state`` there, for the same reason. A module-level event wired to
# ``app.state`` at import is signalled by the first run's teardown and never
# cleared, so a second run in the same process hands every request a signal
# that is already set and cancels each crawl before it does any work.

# Third-party extraction libraries log per-element WARNINGs that are expected
# parser noise (e.g. trafilatura on <a> elements without href) and cannot be
# acted on. Keep ERROR+ visible, drop the chatter.
logging.getLogger("trafilatura").setLevel(logging.ERROR)


def _warn_if_bound_off_loopback(host: str) -> None:
    """Say so at startup when the API is reachable from off the machine.

    Nothing authenticates a caller here: every ``Authorization`` header in the
    tree is outbound, attached to a crawled origin rather than presented by
    one. Loopback is what makes that safe, so the moment the bind leaves
    loopback the whole surface -- crawl execution, job control, result
    retrieval, webhooks -- is open.

    A warning and not a refusal, because the shipped compose file binds
    ``0.0.0.0`` inside the container (Docker port mapping cannot reach a
    loopback-bound process) and publishes only to ``127.0.0.1``. Refusing that
    bind would refuse this project's own supported deployment to guard a
    configuration nobody ships.
    """
    if is_loopback_bind(host):
        return
    logger.warning(
        "Binding to %s puts the crawl API on the network with no caller "
        "authentication: crawl execution, job control, result retrieval and "
        "webhooks are all reachable by anyone who can route to this port. "
        "Bind 127.0.0.1, or keep this address and publish only to loopback "
        "as docker-compose.yml does.",
        (host or "").strip(),
    )


if FastAPI is not None:
    # Declared once here so both this branch and the ``else`` below agree on
    # the type: FastAPI is optional at import time, so the module-level app is
    # ``FastAPI | None`` by construction, not ``FastAPI`` that happens to be
    # None on one path.
    app: FastAPI | None

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # Startup: initialize the browser pool, then load the cookie jar.
        from .crawl.engine import _resource_gate
        from .health.readiness import READINESS_STATE

        # Readiness saturation comes from the live resource gate, not the
        # metrics gauges (which only recursive crawls refresh).
        READINESS_STATE.set_utilization_sampler(
            lambda: (_resource_gate.memory_ratio(), _resource_gate.cpu_ratio())
        )
        # The module-level gate is built at import time with no running
        # loop, so its lag probe must start here.
        _resource_gate.start_lag_probe()

        # Before anything is listening: the exposure is a property of the bind
        # and is true from the first moment, so it is stated before the first
        # request rather than on one.
        _warn_if_bound_off_loopback(settings.host)

        await initialize_browser_pool(settings)
        # Cookies first: recovered jobs can receive cookies in their first
        # batch, and a clear-then-populate load would drop those.
        if settings.cookie_persistence_enabled:
            # The jar is backed by the shared coordination store rather than by
            # a JSON file rewritten at shutdown. That rewrite was replace-
            # everything, so with N workers the last process to exit discarded
            # every cookie the others had collected -- atomic, non-truncated,
            # and still a lost update. The store merges in a transaction
            # instead, so workers accumulate rather than overwrite.
            #
            # An existing cookie_jar.json is imported once, so an upgrade does
            # not silently sign every user out. It is never written again: the
            # file format is a legacy mirror, and keeping it authoritative
            # would keep the last-writer-wins path alive.
            os.makedirs(settings.cache_dir, exist_ok=True)
            from app.crawl.coordination_cookies import SharedCookieStore
            from app.transport import cookie_jar as cookie_jar_module
            from app.transport.cookie_jar import CookieJar

            jar = CookieJar(SharedCookieStore(DATABASE_PATH))
            jar.import_file_into(settings.cookie_jar_path)
            cookie_jar_module._GLOBAL_COOKIE_JAR = jar
            # Published for the same reason as ``shutdown_event`` below: what the
            # process is actually using is otherwise answerable only by reaching
            # for the module global the lifespan just assigned.
            _app.state.cookie_jar = jar
        # /metrics must report the service, not this worker. Counters go to the
        # same shared store as the cookies; the two resource gauges stay
        # process-local because they read a cgroup every worker shares and
        # summing them would report a utilization no process experiences.
        from app.crawl.coordination_counters import CoordinationCounters

        from .metrics import get_metrics

        get_metrics().bind_counters(CoordinationCounters(DATABASE_PATH))

        # Jobs: rewind crash orphans and resume every PENDING whole-site job.
        from .api.job_runner import recover_and_start_pending_jobs

        shutdown_event = asyncio.Event()
        _app.state.shutdown_event = shutdown_event
        await recover_and_start_pending_jobs(job_store, settings, shutdown_event)

        # Watches: the periodic sweep of the due registry, owned by the process
        # that owns the store for the same reason job recovery runs here. Started
        # before the first request, so a registered watch is kept by this
        # deployment rather than by whichever one started last.
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

        # Shutdown: stop the world FIRST so in-flight job crawls can't
        # deliver webhooks against cleared dependencies or mint cookies
        # after the snapshot is taken. Then release webhook
        # deps and close clients. The cookie jar needs no shutdown step: the
        # shared store is the persistence, so there is no whole-jar write left
        # to be last-writer-wins.
        shutdown_event.set()
        from .api.job_runner import shutdown_job_runner

        await shutdown_job_runner()
        # The sweep is mid-fetch the moment this runs, so it is cancelled rather
        # than waited on politely: the event only stops the NEXT pass. Cancelling
        # leaves the watch due, which is the registry's at-least-once contract --
        # the next deployment picks it up -- whereas awaiting a pass to finish
        # would deliver webhooks against the pools closing immediately below.
        watch_sweep_task.cancel()
        try:
            await watch_sweep_task
        except asyncio.CancelledError:
            pass

        from app.transport.http_client_pool import close_async_http_client, close_cffi_session

        from .crawl.engine import _resource_gate

        # Imported here, not at module scope: this pulls in the markitdown
        # extraction stack, and the deferred import is what keeps it off the
        # import path of every CLI and MCP caller that never renders a PDF.
        from .crawl.extraction import shutdown_doc_extraction_executor

        async def _close_gate() -> None:
            _resource_gate.close()

        # Every step is attempted, and a failure is reported only after the last
        # one has run. A close() that raises partway through the list would
        # otherwise cost every pool below it its teardown -- and the
        # document-extraction workers are the one pool nothing ever touches
        # again, so skipping them leaks a markitdown subprocess, its memory and
        # its pipes past the process's own shutdown. The aggregate still raises,
        # so a genuinely broken close is still loud and still names itself.
        await _run_shutdown_steps(
            (
                ("close_async_http_client", close_async_http_client),
                ("close_cffi_session", close_cffi_session),
                ("close_browser_pool", close_browser_pool),
                # The persistent document-extraction subprocesses are the last
                # pool: they are lazy and never touched by the shutdown above,
                # so without this every markitdown worker (and its memory and
                # pipes) outlived the process's own shutdown and was only reaped
                # by the test fixture.
                ("shutdown_doc_extraction_executor", shutdown_doc_extraction_executor),
                ("_resource_gate.close", _close_gate),
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
    # Stash shared dependencies on ``app.state`` so router handlers outside
    # this module can fetch them via :func:`get_settings` /
    # :func:`get_shutdown_event` FastAPI dependencies.
    app.state.crawler_settings = settings
    app.state.job_store = job_store
    app.state.result_store = result_store
    app.state.watch_store = watch_store

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
    app.include_router(watch_router)
    app.include_router(crawl_router)
    app.include_router(policy_router)
    app.include_router(search_router)
    app.include_router(mcp_router)

else:
    # ``FastAPI`` is None here (the optional import failed), so there is no app
    # to build. The module-level ``app`` is None on this path; a reader that
    # needs the optional web surface must handle that, rather than touching a
    # half-initialized module global.
    app = None
