# Lummenna Memory API — production image
#
# Multi-stage, non-root, read-only-friendly. The embedding model is NOT baked
# into the image (models are data, not code): provision it into the models
# volume via the init container / `lummenna model download`, or mount a
# pre-provisioned volume. A pod without a model reports NOT READY at /ready
# rather than serving degraded results.
#
# Build:  docker build -t lummenna:latest .
# Run:    docker run -p 8848:8848 -v lummenna-data:/data/store -v lummenna-models:/data/models lummenna:latest

# --- Build stage -----------------------------------------------------------
FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml PACKAGE-README.md LICENSE ./
COPY lummenna/ lummenna/
COPY benchmarks/retrieval/results/retrieval_results.json benchmarks/retrieval/results/retrieval_results.json
COPY benchmarks/beir/results/beir_results.json benchmarks/beir/results/beir_results.json
COPY benchmarks/e2e/results/e2e_results.json benchmarks/e2e/results/e2e_results.json

# Build a wheel and install into an isolated prefix that the runtime copies.
RUN pip install --prefix=/install '.[azure]'

# --- Runtime stage ---------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Lummenna Memory API"
LABEL org.opencontainers.image.description="Local-first memory and context framework for sovereign AI agents"
LABEL org.opencontainers.image.source="https://lummenna.com/community"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LUMMENNA_LOG_LEVEL=info \
    LUMMENNA_API_HOST=0.0.0.0 \
    LUMMENNA_API_PORT=8848 \
    LUMMENNA_STORE_PATH=/data/store \
    LUMMENNA_MODEL_PATH=/data/models \
    FORWARDED_ALLOW_IPS=127.0.0.1

# FORWARDED_ALLOW_IPS: rate limiting and brute-force throttling key on the
# client IP uvicorn reports. Behind a reverse proxy/ingress that is the
# PROXY's IP unless X-Forwarded-For is trusted — which would throttle ALL
# customers as one shared bucket. uvicorn reads FORWARDED_ALLOW_IPS (IPs or
# CIDRs of trusted proxies) natively; set it to your ingress/LB range at
# runtime, e.g. -e FORWARDED_ALLOW_IPS=10.0.0.0/8 for cluster-internal LBs.

COPY --from=build /install /usr/local

# Non-root user; /data owned by it for the store and models volumes.
RUN groupadd --gid 10001 lummenna \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lummenna \
    && mkdir -p /data/store /data/models \
    && chown -R lummenna:lummenna /data

USER 10001:10001

CMD ["uvicorn", "lummenna.api.server:app", "--host", "0.0.0.0", "--port", "8848", "--log-level", "info", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]

EXPOSE 8848

# Liveness only — Kubernetes readiness uses /ready (model-aware).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8848/health')" || exit 1

