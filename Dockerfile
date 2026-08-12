# syntax=docker/dockerfile:1.7
ARG BROWSER_CRAWLER_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BROWSER_CRAWLER_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1   PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip   pip install -r /app/requirements.txt

# Pre-fetch the Camoufox browser binary EARLY. This is a slow network download
# and must stay cached across rebuilds, so it is placed immediately after the
# pip install (which provides the `camoufox` package) and BEFORE the apt/system
# dependency layer and the COPY app layer. Adding or changing system packages
# or application code therefore never invalidates this download.
RUN python -m camoufox fetch

# System dependencies. The browser runtime needs the X11/xcb and NSS libraries
# to load libxul.so; without them Camoufox fails to launch ("libX11-xcb.so.1:
# cannot open shared object file"). Placed AFTER the Camoufox download so that
# edits here never re-trigger the browser re-download.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked   --mount=type=cache,target=/var/lib/apt,sharing=locked   apt-get update   && apt-get install -y --no-install-recommends     libgtk-3-0 libasound2 libdbus-glib-1-2 libxt6 libxtst6 libxrandr2 libgbm1     ca-certificates curl fonts-liberation poppler-utils     libx11-xcb1 libxcomposite1 libxdamage1 libxfixes3 libxcb1 libxcb-shm0     libxcb-render0 libxext6 libx11-6 libglib2.0-0 libstdc++6 libatk1.0-0     libatk-bridge2.0-0 libcups2 libpango-1.0-0 libcairo2 libnss3 libnspr4     libdbus-1-3 libxkbcommon0 libxshmfence1 libfontconfig1

COPY app /app/app
COPY --from=shared local_service_logging.py /app/local_service_logging.py
RUN mkdir -p /app/logs

EXPOSE 8765
CMD uvicorn app.main:app --host 0.0.0.0 --port 8765 --log-level ${BROWSER_CRAWLER_LOG_LEVEL:-info}
