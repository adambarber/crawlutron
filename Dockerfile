# syntax=docker/dockerfile:1.7
# Build context is this repository's root, where this Dockerfile lives, so the
# build is `docker build .` (or `docker compose build`) from the repository root
# with no extra flags. `.dockerignore` is written for that same root: it
# allowlists the four things the stages below COPY, which holds the build
# context to ~2.6 MB instead of the whole working tree.
ARG BROWSER_CRAWLER_BASE_IMAGE=python:3.12-slim-bookworm

# ==========================================
# 1. Builder Stage: Compile Python dependencies
# ==========================================
FROM ${BROWSER_CRAWLER_BASE_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install build dependencies required for compiling Python wheels
# (e.g., lxml, charset-normalizer, brotli, pydantic-core)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential python3-dev libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies to a standard local directory
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# ==========================================
# 2b. Isolated Camoufox browser-binary stage
# ==========================================
# The browser binary download (~1.3 GB) is isolated into its own stage that
# depends ONLY on the camoufox requirement line. Editing other requirements
# (e.g. adding h2) therefore cannot invalidate this download, eliminating the
# >10 minute rebuilds previously caused by a full layer-cache miss.
#
# NOTE: ``COPY --from`` can only see the stage's committed layer filesystem,
# NOT BuildKit cache-mount contents (cache mounts are detached when the RUN
# exits). The fetch therefore runs inside a cache mount for rebuild speed,
# and the result is copied into the stage filesystem in the same RUN so the
# runtime stage can pull it out via ``COPY --from=camoufox-binary``.
FROM ${BROWSER_CRAWLER_BASE_IMAGE} AS camoufox-binary

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir "$(grep -iE '^camoufox' requirements.txt)"

# Fetch the browser binary with a persistent cache mount as a safety net:
# if the layer cache is pruned but the build cache survives, ``fetch``
# short-circuits and the bundle layer is rebuilt cheaply from warm cache.
RUN --mount=type=cache,target=/root/.cache/camoufox,sharing=locked \
    python -m camoufox fetch \
    && test -d /root/.cache/camoufox \
    && mkdir -p /camoufox-bundle \
    && cp -a /root/.cache/camoufox/. /camoufox-bundle/

# ==========================================
# 2. Runtime Stage: Lean production image
# ==========================================
FROM ${BROWSER_CRAWLER_BASE_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PYTHONPATH=/app \
    TZ=UTC \
    PATH=/home/appuser/.local/bin:$PATH \
    BROWSER_CRAWLER_LOG_LEVEL=info \
    # Single worker so the browser pool is one GLOBAL pool (per-worker pools would
    # multiply the browser count by the worker count and blow the RAM budget).
    # The pool size is then derived from the cgroup memory limit at startup.
    # Scale horizontally via containers if more browser concurrency is needed.
    BROWSER_CRAWLER_WORKERS=1 \
    # Opt-in in-container disk content cache (#5): cache successful HTTP/Browser
    # responses on disk (under BROWSER_CRAWLER_CACHE_DIR) to avoid re-download/
    # re-extract on repeat crawls. No external sidecar (Redis/Memcached) required.
    BROWSER_CRAWLER_CONTENT_CACHE_ENABLED=true \
    BROWSER_CRAWLER_CACHE_DIR=/app/cache \
    BROWSER_CRAWLER_CACHE_TTL_SECONDS=3600 \
    # Zero telemetry is a hard posture for this service. ONNX Runtime (pulled in
    # transitively by magika, used for ML content-type inference) is the one
    # component here that has a native opt-out flag, so we set it at the source.
    # Playwright ships no telemetry in this version and camoufox only phone-homes
    # on demand; the compose extra_hosts sinkhole is the un-bypassable chokepoint
    # for any of those that lack a flag. See
    # docs/references/browser-crawler-telemetry.md.
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
    libgtk-3-0 libasound2 libdbus-glib-1-2 libxt6 libxtst6 libxrandr2 libgbm1 \
    ca-certificates curl fonts-liberation fonts-noto-core poppler-utils \
    libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 libxcb1 libxcb-shm0 \
    libxcb-render0 libxext6 libx11-6 libglib2.0-0 libstdc++6 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libpango-1.0-0 libcairo2 libnss3 libnspr4 \
    libdbus-1-3 libxkbcommon0 libxshmfence1 libfontconfig1 \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*


# Create a non-root user for security
RUN useradd -m -s /bin/bash appuser \
    && mkdir -p /app/logs /app/cache \
    && chown -R appuser:appuser /app

# Switch to non-root user
USER appuser
ENV HOME=/home/appuser

# Copy installed Python packages from builder stage to the appuser's local directory
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Bring in the pre-fetched Camoufox browser binary from the earlier
# ``camoufox-binary`` build stage. This avoids the circular ``--from=
# browser-crawler:latest`` reference that previously broke fresh-clone builds
# (the source image had not been tagged yet). The isolated stage depends only
# on the camoufox package line, so editing other requirements cannot
# invalidate the browser download cache.
COPY --from=camoufox-binary /camoufox-bundle /home/appuser/.cache/camoufox

# Copy application code. The logging helper is in-tree at
# ``app/logging_config.py`` (no external shared-tree copy is needed).
COPY --chown=appuser:appuser app /app/app

# uvicorn applies its logging config in ``Config.__init__`` -- BEFORE the app
# is imported -- so the service's JSON sink must exist by then or uvicorn's own
# startup lines stay plain text. This config installs the root JSON handler and
# points uvicorn's loggers at it; ``app.main`` adds the env-configured rotating
# file sink on import (and skips the stream handler as already present).
COPY --chown=appuser:appuser uvicorn-log-config.json /app/uvicorn-log-config.json

EXPOSE 8765

# Add a healthcheck based on the FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

# Production command. The bind interface defaults to loopback (M15: the service
# has no caller authentication); BROWSER_CRAWLER_HOST must be set explicitly to
# expose the port (the compose file sets 0.0.0.0, gated by its loopback mapping).
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${BROWSER_CRAWLER_HOST:-127.0.0.1} --port 8765 --workers ${BROWSER_CRAWLER_WORKERS} --log-config /app/uvicorn-log-config.json --log-level ${BROWSER_CRAWLER_LOG_LEVEL}"]

# ==========================================
# 3. Development Stage: Hot-reloading and dev tools
# ==========================================
FROM runtime AS development

# Switch back to root to install dev dependencies
USER root

# Install development tools (pytest, pytest-asyncio, ruff, etc.)
COPY requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-dev.txt

# Switch back to appuser
USER appuser

# Development command with hot-reloading and debug logging
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${BROWSER_CRAWLER_HOST:-127.0.0.1} --port 8765 --reload --log-config /app/uvicorn-log-config.json --log-level debug"]
