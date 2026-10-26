FROM python:3.12-slim

ARG LUMMENNA_VERSION=1.0.1rc1
ARG LUMMENNA_WHEEL_URL=https://lummenna.com/downloads/lummenna-1.0.1rc1-py3-none-any.whl
ARG LUMMENNA_WHEEL_SHA256=7d7aa3e88f8bf15899439a978d23650db7bf7235953652c56bfef26be18eb97d

LABEL org.opencontainers.image.title="Lummenna Community Sidecar"
LABEL org.opencontainers.image.version="1.0.1rc1"
LABEL org.opencontainers.image.source="https://lummenna.com/community"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LUMMENNA_API_HOST=0.0.0.0 \
    LUMMENNA_API_PORT=8848 \
    LUMMENNA_STORE_PATH=/data/store \
    LUMMENNA_MODEL_PATH=/data/models \
    LUMMENNA_SOVEREIGN=true

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates cmake curl \
    && wheel="/tmp/lummenna-${LUMMENNA_VERSION}-py3-none-any.whl" \
    && curl --fail --show-error --silent --location "$LUMMENNA_WHEEL_URL" --output "$wheel" \
    && printf '%s  %s\n' "$LUMMENNA_WHEEL_SHA256" "$wheel" | sha256sum --check --strict \
    && python -m pip install --no-cache-dir "${wheel}[export]" \
    && rm -f "$wheel" \
    && apt-get purge -y --auto-remove build-essential cmake curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 lummenna \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin lummenna \
    && mkdir -p /data/store /data/models \
    && chown -R lummenna:lummenna /data \
    && printf '%s\n' \
       '#!/bin/sh' \
       'set -eu' \
       ': "${LUMMENNA_API_KEY:?LUMMENNA_API_KEY is required}"' \
       'exec "$@"' \
       > /usr/local/bin/lummenna-entrypoint \
    && chmod 0755 /usr/local/bin/lummenna-entrypoint

USER 10001:10001

VOLUME ["/data/store", "/data/models"]
EXPOSE 8848

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8848/health')" || exit 1

ENTRYPOINT ["/usr/local/bin/lummenna-entrypoint"]
CMD ["lummenna", "serve", "--host", "0.0.0.0", "--port", "8848"]
