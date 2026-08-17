# syntax=docker/dockerfile:1.7
# Build context is the `docker/` directory (parent of all services) so the shared
# local_service_logging.py module is reachable without an external build context.
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
COPY browser-crawler/requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --user -r requirements.txt

# ==========================================
# 2b. Isolated Camoufox browser-binary stage
# ==========================================
# The browser binary download (~1.3 GB) is isolated into its own stage that
# depends ONLY on the camoufox requirement line. Editing other requirements
# (e.g. adding h2) therefore cannot invalidate this download, eliminating the
# >10 minute rebuilds previously caused by a full layer-cache miss.
FROM ${BROWSER_CRAWLER_BASE_IMAGE} AS camoufox-binary

WORKDIR /app
COPY browser-crawler/requirements.txt .
RUN pip install --no-cache-dir "$(grep -iE '^camoufox' requirements.txt)"
RUN python -m camoufox fetch

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
    # Set workers to 1 by default for browser automation to prevent memory exhaustion.
    # Scale horizontally via containers if more concurrency is needed.
    BROWSER_CRAWLER_WORKERS=1 \
    # Opt-in in-container disk content cache (#5): cache successful HTTP/Browser
    # responses on disk (under BROWSER_CRAWLER_CACHE_DIR) to avoid re-download/
    # re-extract on repeat crawls. No external sidecar (Redis/Memcached) required.
    BROWSER_CRAWLER_CONTENT_CACHE_ENABLED=true \
    BROWSER_CRAWLER_CACHE_DIR=/app/cache \
    BROWSER_CRAWLER_CACHE_TTL_SECONDS=3600

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

# Bring in the pre-fetched Camoufox browser binary from the isolated stage
# (committed to the image) so the runtime does not re-download it.
COPY --from=camoufox-binary --chown=appuser:appuser /root/.cache/camoufox /home/appuser/.cache/camoufox

# Copy application code and the shared logging module (no external build context needed)
COPY --chown=appuser:appuser browser-crawler/app /app/app
COPY --chown=appuser:appuser shared/python/local_service_logging.py /app/local_service_logging.py

EXPOSE 8765

# Add a healthcheck based on the FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8765/health || exit 1

# Production command
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8765 --workers ${BROWSER_CRAWLER_WORKERS} --log-level ${BROWSER_CRAWLER_LOG_LEVEL}"]

# ==========================================
# 3. Development Stage: Hot-reloading and dev tools
# ==========================================
FROM runtime AS development

# Switch back to root to install dev dependencies
USER root

# Install development tools (pytest, pytest-asyncio, ruff, etc.)
COPY browser-crawler/requirements-dev.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-dev.txt

# Switch back to appuser
USER appuser

# Development command with hot-reloading and debug logging
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8765 --reload --log-level debug"]
