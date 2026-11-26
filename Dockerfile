# syntax=docker/dockerfile:1

# One base image for both stages: the virtualenv links to the builder's interpreter
# (/usr/local/bin/python3.x), so the runtime must have exactly the same one.
ARG PYTHON_IMAGE=python:3.15-slim-trixie

# --- Build: resolve dependencies into a virtualenv with uv ---------------------------------------
FROM ${PYTHON_IMAGE} AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Dependencies first, in their own layer: code changes don't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev

# --- Runtime: no uv, no build cache, non-root ----------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

# Owned by root: the app user can read the code but not change it.
COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src

# Last, so a new commit rebuilds only this layer, not the dependency layers above.
ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA}
LABEL org.opencontainers.image.revision=${GIT_SHA}

USER app

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"]

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
