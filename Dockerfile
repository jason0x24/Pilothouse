# Multi-stage Dockerfile for the Pilothouse API server.
#
# Build stage compiles a wheel; runtime stage installs only the wheel
# into a slim Python base. The result is ~150 MB and contains no build
# toolchain. The console (Next.js) ships separately — see console/Dockerfile.

FROM python:3.12-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /src
RUN pip install --upgrade pip build hatchling

# Copy only what's needed to build a wheel — keeps the layer small and
# stable for caching. Editable installs aren't used in the runtime image.
COPY pyproject.toml README.md ./
COPY pilothouse ./pilothouse

RUN python -m build --wheel --outdir /wheels


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PILOTHOUSE_DATA_DIR=/data \
    PILOTHOUSE_HOST=0.0.0.0 \
    PILOTHOUSE_PORT=8088

# Postgres support is optional but cheap to bake in; many real deploys
# replace SQLite with Postgres via PILOTHOUSE_DATABASE_URL.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — the runtime never needs root.
RUN useradd --system --uid 10001 --home-dir /home/pilothouse pilothouse \
    && mkdir -p /data /home/pilothouse \
    && chown -R pilothouse:pilothouse /data /home/pilothouse

COPY --from=build /wheels/*.whl /tmp/
# `asyncpg` powers the SQLAlchemy `postgresql+asyncpg://` driver used by
# the docker-compose stack. SQLite users (the dev default) get nothing
# extra — the wheel itself depends only on aiosqlite.
RUN pip install /tmp/*.whl 'asyncpg>=0.29' \
    && rm /tmp/*.whl

USER pilothouse
WORKDIR /home/pilothouse
EXPOSE 8088
VOLUME ["/data"]

# `pilothouse serve` reads PILOTHOUSE_HOST/PORT from env and runs
# uvicorn under the hood. We don't run init_db at container start —
# the lifespan does it idempotently on the first HTTP request anyway.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8088/healthz || exit 1

ENTRYPOINT ["pilothouse"]
CMD ["serve"]
