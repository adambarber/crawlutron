# syntax=docker/dockerfile:1.7
ARG BROWSER_CRAWLER_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BROWSER_CRAWLER_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1   PYTHONUNBUFFERED=1

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked   --mount=type=cache,target=/var/lib/apt,sharing=locked   apt-get update   && apt-get install -y --no-install-recommends \
    libgtk-3-0 libasound2 libdbus-glib-1-2 libxt6 libxtst6 libxrandr2 libgbm1 \
    ca-certificates curl fonts-liberation poppler-utils

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip   pip install -r /app/requirements.txt
COPY app /app/app
COPY --from=shared local_service_logging.py /app/local_service_logging.py
RUN mkdir -p /app/logs

# Pre-fetch Camoufox binary
RUN python -c "from camoufox.sync_api import Camoufox; Camoufox().start().stop()"

EXPOSE 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--log-level", "${BROWSER_CRAWLER_LOG_LEVEL:-info}"]
