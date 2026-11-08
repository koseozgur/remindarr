# Two stages: build the venv with uv, then copy just the venv into a clean
# runtime image. uv itself and the build cache never reach the final layer.

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# source, so editing a .py file does not re-resolve the whole dependency tree.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm AS runtime

# uid 1000 is the conventional first non-root user and matches the default on
# most NAS setups. If your bind-mounted ./data is owned by something else, see
# the note in the README rather than switching this to root.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home app \
    && mkdir -p /data \
    && chown -R app:app /data

COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    REMINDARR_DATABASE_URL=sqlite+aiosqlite:////data/remindarr.db

WORKDIR /app
USER app
VOLUME ["/data"]

# Telegram long-polls and Discord uses its gateway, both outbound. Only LINE
# needs to be reached from outside: its webhook listens here when LINE is
# configured (REMINDARR_WEBHOOK_PORT). EXPOSE documents that; it publishes
# nothing.
EXPOSE 8080

ENTRYPOINT ["remindarr"]
