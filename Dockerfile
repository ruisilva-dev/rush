# ==============================================================================
# Stage 1: Build & Dependency Resolution
# Uses a pinned uv binary and cache mount for fast, immutable builds.
# ==============================================================================
FROM python:3.12-slim-trixie AS builder

# Pin uv version for reproducible builds
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./

# ------------------------------------------------------------------------------
# Install runtime dependencies into /app/.venv:
# - Mounts a persistent build cache so wheel downloads are reused across
#   rebuilds without leaking cached files into the resulting image layer.
# - --frozen: Fails the build if pyproject.toml and uv.lock are out of sync.
# - --no-dev: Excludes development tools (ruff, pytest, mypy).
# - --no-install-project: Installs third-party packages only, allowing Docker
#   to cache this expensive layer before copying application source code.
# ------------------------------------------------------------------------------
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project


# ==============================================================================
# Alternate Stage: Test Suite
# Inherits the pinned uv environment and cached files from 'builder'.
# Runs uv sync without '--no-dev' to install testing tools (pytest, ruff).
# ==============================================================================
FROM builder AS test

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Add the test files and application code
COPY ./app ./app
COPY ./tests ./tests

ENV PATH="/app/.venv/bin:$PATH"

CMD ["pytest"]

# ==============================================================================
# Stage 2: Minimal Hardened Runtime
# Zero build tools and drops root privileges.
# ==============================================================================
FROM python:3.12-slim-trixie AS runner

WORKDIR /app

# Create a non-privileged system user
RUN groupadd -r appgroup && useradd -r -g appgroup -s /sbin/nologin appuser

# NOTE: Apply --chown directly during COPY. A separate 'RUN chown' would
# create a duplicate copy of these files and double the image layer size.
COPY --from=builder --chown=appuser:appgroup /app/.venv /app/.venv
COPY --chown=appuser:appgroup ./app ./app

# Prepend the venv to PATH so binaries run directly without needing 'source activate'
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Drop root privileges
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
