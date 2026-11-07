# syntax=docker/dockerfile:1.7
# Build context is this repository's root, where this Dockerfile lives, so the
# build is `docker build .` (or `docker compose build`) from the repository root
# with no extra flags. `.dockerignore` is written for that same root: it
# allowlists the five things the stages below COPY, which holds the build
# context to ~2.6 MB instead of the whole working tree.
#
# The base image is pinned by tag AND digest (CRAWLUTRON_BASE_IMAGE below) so a
# rebuilt image is byte-for-byte reproducible and a fast-moving upstream tag can
# never silently roll the deployment forward. The digest is the multi-arch OCI
# index captured 2026-11-02. A further major bump (Python 3.15) is deferred: the
# frozen lock cannot resolve on 3.15 until upstream ships cp315 wheels.
ARG CRAWLUTRON_BASE_IMAGE=python:3.14.10-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527

# ==========================================
# 1. Builder Stage: Compile Python dependencies
# ==========================================
FROM ${CRAWLUTRON_BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install uv from its official image. The tag matches the uv pinned by the
# project's own tooling (uv 0.13.6), so the builder and the Astral image agree.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

# Install build dependencies required for compiling Python wheels
# (e.g., lxml, charset-normalizer, brotli, pydantic-core)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential=12.9 python3-dev=3.11.2-1+b1 \
    libxml2-dev=2.9.14+dfsg-1.3~deb12u6 libxslt1-dev=1.1.35-1+deb12u4 \
    && rm -rf /var/lib/apt/lists/*

# pyproject.toml + uv.lock are the single source of truth for the dependency
# graph (requirements*.txt were deleted in dc6f2bbe). ``uv sync --frozen``
# installs exactly the locked pins, with no hash-check drift.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python

# ==========================================
# 2b. Isolated Camoufox browser-binary stage
# ==========================================
# The browser binary download (~1.3 GB) is isolated into its own stage that
# depends ONLY on the camoufox requirement line. Editing other dependencies
# (e.g. adding h2) therefore cannot invalidate this download, eliminating the
# >10 minute rebuilds previously caused by a full layer-cache miss.
#
# NOTE: ``COPY --from`` can only see the stage's committed layer filesystem,
# NOT BuildKit cache-mount contents (cache mounts are detached when the RUN
# exits). The fetch therefore runs inside a cache mount for rebuild speed,
# and the result is copied into the stage filesystem in the same RUN so the
# runtime stage can pull it out via ``COPY --from=camoufox-binary``.
FROM ${CRAWLUTRON_BASE_IMAGE} AS camoufox-binary

# The pin-extraction pipeline below greps the frozen lock; force bash+pipefail
# so a failing ``uv export`` fails the RUN instead of silently producing empty.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./

# Install ONLY the camoufox pin into a dedicated dir so the fetch RUN below has
# the module on PYTHONPATH. The pin is extracted from the frozen lock (single
# source of truth); the ``awk`` step drops uv's trailing line-continuation.
RUN uv pip install --no-cache-dir --python /usr/local/bin/python \
    --target /camoufox-deps \
    "$(uv export --frozen | grep -iE '^camoufox' | awk '{print $1}')"

# Fetch the browser binary with a persistent cache mount as a safety net:
# if the layer cache is pruned but the build cache survives, ``fetch``
# short-circuits and the bundle layer is rebuilt cheaply from warm cache.
# camoufox writes its install under user_cache_dir("camoufox") and manages that
# dir itself (rmtree on replace). Pointing it at the buildkit cache MOUNT root
# breaks: camoufox's own cleanup runs on the mount while buildkit holds it open
# -> errno 16 "Device or resource busy". Point XDG_CACHE_HOME at a path INSIDE
# the mount (distinct from the raw fetch cache) so camoufox owns a real dir and
# the mount only persists the finished install across rebuilds.
#
# OFFLINE SEED (option B): a named `seed` build context (a real camoufox cache,
# e.g. ~/Library/Caches/camoufox) is bound directly by its name and copied into
# the mount so the fetch short-circuits without the network; an empty seed dir
# (compose's default) hits the `if [ -d /seed ]` guard and the RUN fetches over
# the wire exactly as before. The context is MANDATORY: a build without
# --build-context seed= makes BuildKit resolve docker.io/library/seed and fail
# before this RUN starts (#625, probed live) -- compose supplies
# .dockerseed by default; bare builds pass --build-context seed=<dir>.
RUN --mount=type=cache,target=/root/.cache/camoufox-mount,sharing=locked \
    --mount=type=bind,from=seed,target=/seed \
    sh -c 'if [ -d /seed ]; then mkdir -p /root/.cache/camoufox-mount && (cp -a /seed/camoufox /root/.cache/camoufox-mount/ 2>/dev/null || cp -a /seed/. /root/.cache/camoufox-mount/camoufox/); fi; PYTHONPATH=/camoufox-deps XDG_CACHE_HOME=/root/.cache/camoufox-mount python -m camoufox fetch; test -d /root/.cache/camoufox-mount/camoufox && mkdir -p /camoufox-bundle && cp -a /root/.cache/camoufox-mount/camoufox/. /camoufox-bundle/'

# ==========================================
# 2. Runtime Stage: Lean production image
# ==========================================
FROM ${CRAWLUTRON_BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PYTHONPATH=/app \
    TZ=UTC \
    PATH=/app/.venv/bin:$PATH \
    CRAWLUTRON_LOG_LEVEL=info \
    # Single worker so the browser pool is one GLOBAL pool (per-worker pools would
    # multiply the browser count by the worker count and blow the RAM budget).
    # The pool size is then derived from the cgroup memory limit at startup.
    # Scale horizontally via containers if more browser concurrency is needed.
    CRAWLUTRON_WORKERS=1 \
    # Opt-in in-container disk content cache (#5): cache successful HTTP/Browser
    # responses on disk (under CRAWLUTRON_CACHE_DIR) to avoid re-download/
    # re-extract on repeat crawls. No external sidecar (Redis/Memcached) required.
    CRAWLUTRON_CONTENT_CACHE_ENABLED=true \
    CRAWLUTRON_CACHE_DIR=/app/cache \
    CRAWLUTRON_CACHE_TTL_SECONDS=3600 \
    # Zero telemetry is a hard posture for this service. ONNX Runtime (pulled in
    # transitively by magika, used for ML content-type inference) is the one
    # component here that has a native opt-out flag, so we set it at the source.
    # Playwright ships no telemetry in this version and camoufox only phone-homes
    # on demand; the compose extra_hosts sinkhole is the un-bypassable chokepoint
    # for any of those that lack a flag. See
    # docs/references/crawlutron-telemetry.md.
    ORT_DISABLE_TELEMETRY=1 \
    # Bound glibc malloc arenas: concurrent lxml/trafilatura parsing otherwise
    # inflates resident memory via arena fragmentation.
    MALLOC_ARENA_MAX=2

WORKDIR /app

# Install runtime system dependencies.
# ADDED: libxml2, libxslt1.1 (required by lxml/trafilatura at runtime)
# ADDED: fonts-noto-core (improves rendering accuracy for non-Latin municipal sites)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0=3.24.38-2~deb12u3 libasound2=1.2.8-1+b1 \
    libdbus-glib-1-2=0.112-3 libxt6=1:1.2.1-1.1 libxtst6=2:1.2.3-1.1 \
    libxrandr2=2:1.5.2-2+b1 libgbm1=22.3.6-1+deb12u2 \
    ca-certificates=20250419~deb12u2 curl=7.88.1-10+deb12u15 \
    fonts-liberation=1:1.07.4-11 fonts-noto-core=20201225-1 \
    poppler-utils=22.12.0-2+deb12u3 libx11-xcb1=2:1.8.4-2+deb12u2 \
    libxcomposite1=1:0.4.5-1 libxdamage1=1:1.1.6-1 libxfixes3=1:6.0.0-2 \
    libxcb1=1.15-1 libxcb-shm0=1.15-1 libxcb-render0=1.15-1 \
    libxext6=2:1.3.4-1+b1 libx11-6=2:1.8.4-2+deb12u2 libglib2.0-0=2.74.6-2+deb12u9 \
    libstdc++6=12.2.0-14+deb12u1 libatk1.0-0=2.46.0-5 libatk-bridge2.0-0=2.46.0-5 \
    libcups2=2.4.2-3+deb12u9 libpango-1.0-0=1.50.12+ds-1 libcairo2=1.16.0-7 \
    libnss3=2:3.87.1-1+deb12u4 libnspr4=2:4.35-1 libdbus-1-3=1.14.10-1~deb12u1 \
    libxkbcommon0=1.5.0-1 libxshmfence1=1.3-1 libfontconfig1=2.14.1-4 \
    libxml2=2.9.14+dfsg-1.3~deb12u6 libxslt1.1=1.1.35-1+deb12u4 \
    && rm -rf /var/lib/apt/lists/*


# Create a non-root user for security
RUN useradd -m -s /bin/bash appuser \
    && mkdir -p /app/logs /app/cache \
    && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser
ENV HOME=/home/appuser

# Copy the uv-resolved venv from the builder stage (the appuser owns it).
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# Bring in the pre-fetched Camoufox browser binary from the earlier
# ``camoufox-binary`` build stage. This avoids the circular ``--from=
# crawlutron:latest`` reference that previously broke fresh-clone builds
# (the source image had not been tagged yet). The isolated stage depends only
# on the camoufox package line, so editing other requirements cannot
# invalidate the browser download cache.
COPY --from=camoufox-binary /camoufox-bundle /home/appuser/.cache/camoufox

# Copy application code. The logging helper is in-tree at
# ``crawlutron/runtime/logging_config.py`` (no external shared-tree copy is needed).
COPY --chown=appuser:appuser crawlutron /app/crawlutron

# uvicorn applies its logging config in ``Config.__init__`` -- BEFORE the app
# is imported -- so the service's JSON sink must exist by then or uvicorn's own
# startup lines stay plain text. This config installs the root JSON handler and
# points uvicorn's loggers at it; ``crawlutron.main`` adds the env-configured rotating
# file sink on import (and skips the stream handler as already present).
COPY --chown=appuser:appuser uvicorn-log-config.json /app/uvicorn-log-config.json

EXPOSE 8765

# Add a healthcheck based on the FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

# Production command. The bind interface defaults to loopback (callers
# authenticate with an API key; see SECURITY.md); CRAWLUTRON_HOST must be set explicitly to
# expose the port (the compose file sets 0.0.0.0, gated by its loopback mapping).
CMD ["sh", "-c", "exec uvicorn crawlutron.main:app --host ${CRAWLUTRON_HOST:-127.0.0.1} --port 8765 --workers ${CRAWLUTRON_WORKERS} --log-config /app/uvicorn-log-config.json --log-level ${CRAWLUTRON_LOG_LEVEL}"]

# ==========================================
# 3. Development Stage: Hot-reloading and dev tools
# ==========================================
FROM runtime AS development

# Switch back to root to install dev dependencies
USER root

# Install uv and sync the dev extra (pytest, pytest-asyncio, ruff, etc.) into
# the venv the runtime stage already copied in, on top of the exact locked pins.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --no-install-project --python /app/.venv/bin/python

# Switch back to appuser
USER appuser

# Development command with hot-reloading and debug logging
CMD ["sh", "-c", "exec uvicorn crawlutron.main:app --host ${CRAWLUTRON_HOST:-127.0.0.1} --port 8765 --reload --log-config /app/uvicorn-log-config.json --log-level debug"]