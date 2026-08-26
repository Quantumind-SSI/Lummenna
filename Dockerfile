# Lumena Memory API — production image
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
COPY pyproject.toml README.md LICENSE ./
COPY lummenna/ lummenna/

# Build a wheel and install into an isolated prefix that the runtime copies.
RUN pip install --prefix=/install .

# --- Runtime stage ---------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Lumena Memory API"
LABEL org.opencontainers.image.description="Local-first memory and context framework for sovereign AI agents"
LABEL org.opencontainers.image.source="https://github.com/QuantmindSSI/lummenna"
LABEL org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LUMENA_LOG_LEVEL=info \
    LUMENA_API_HOST=0.0.0.0 \
    LUMENA_API_PORT=8848 \
    LUMENA_STORE_PATH=/data/store \
    LUMENA_MODEL_PATH=/data/models

COPY --from=build /install /usr/local

# Non-root user; /data owned by it for the store and models volumes.
RUN groupadd --gid 10001 lummenna \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lummenna \
    && mkdir -p /data/store /data/models \
    && chown -R lummenna:lummenna /data

USER 10001:10001

VOLUME ["/data/store", "/data/models"]

EXPOSE 8848

# Liveness only — Kubernetes readiness uses /ready (model-aware).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8848/health')" || exit 1

CMD ["uvicorn", "lummenna.api.server:app", "--host", "0.0.0.0", "--port", "8848", "--log-level", "info"]
