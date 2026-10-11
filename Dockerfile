# Multi-stage image for the tradedesk-mcp server.
#
# Build:   docker build -t tradedesk-mcp .
# Run:     see docker-compose.yml, or
#            docker run --rm -it \
#              -p 127.0.0.1:8000:8000 \
#              -v $PWD/data:/data \
#              -e TRADEDESK_TRANSPORT=streamable-http \
#              -e TRADEDESK_HOST=0.0.0.0 \
#              -e TRADEDESK_DB_PATH=/data/tradedesk.sqlite \
#              -e TRADEDESK_COLLECTOR_ENABLED=1 \
#              tradedesk-mcp

ARG PYTHON_VERSION=3.12

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

WORKDIR /src

# Install deps first for layer caching.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Project source.
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 tradedesk \
 && useradd  --system --uid 10001 --gid 10001 --home /app --shell /sbin/nologin tradedesk \
 && mkdir -p /data \
 && chown tradedesk:tradedesk /data

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    TRADEDESK_TRANSPORT=streamable-http \
    TRADEDESK_HOST=0.0.0.0 \
    TRADEDESK_PORT=8000 \
    TRADEDESK_DB_PATH=/data/tradedesk.sqlite \
    TRADEDESK_COLLECTOR_ENABLED=1

WORKDIR /app
COPY --from=builder --chown=tradedesk:tradedesk /src/.venv /app/.venv
COPY --from=builder --chown=tradedesk:tradedesk /src/src /app/src
COPY --chown=tradedesk:tradedesk deploy /app/deploy

USER tradedesk
VOLUME ["/data"]
EXPOSE 8000

# TCP-level healthcheck: streamable-http MCP has no /healthz, but a bound port
# is the right basic signal. Replace with a /mcp handshake once the SDK exposes one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), timeout=3)" || exit 1

CMD ["tradedesk-mcp"]
