# syntax=docker/dockerfile:1.7
ARG BROWSER_CRAWLER_BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BROWSER_CRAWLER_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
  PYTHONUNBUFFERED=1

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
  --mount=type=cache,target=/var/lib/apt,sharing=locked \
  apt-get update \
  && apt-get install -y --no-install-recommends chromium chromium-driver ca-certificates curl fonts-liberation poppler-utils xvfb

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip \
  pip install -r /app/requirements.txt
COPY app /app/app

EXPOSE 8765
CMD ["sh", "-c", "if [ \"${BROWSER_CRAWLER_HEADLESS:-true}\" = \"false\" ]; then Xvfb :99 -screen 0 1365x768x24 -nolisten tcp >/tmp/browser-crawler-xvfb.log 2>&1 & export DISPLAY=:99; fi; exec uvicorn app.main:app --host 0.0.0.0 --port 8765"]
