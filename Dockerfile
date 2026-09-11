# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Never run as root: the container holds a signing key.
RUN useradd --create-home --uid 10001 deepflow
USER deepflow

# PAPER by default. LIVE additionally requires the confirmation flag, the
# exact ack phrase, and credentials -- see config/settings.py.
ENV DEEPFLOW_MODE=PAPER

CMD ["deepflow"]
