# Multi-stage Dockerfile using Astral uv and Python 3.12-slim
FROM python:3.12-slim AS base

# Install uv from official Astral binary image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set working directory and standard Python environment variables
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Stage 1: Dependency builder stage for fast layer caching
FROM base AS builder

WORKDIR /app

# Install dependencies using uv.lock and pyproject.toml
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Stage 2: Final runtime container
FROM base AS runner

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /app/.venv /app/.venv

# Copy workspace source files
COPY . .

# Complete project installation inside the virtual environment
RUN uv sync --frozen --no-dev

# Build arguments for service name and port
ARG SERVICE_DIR=api_gateway
ARG PORT=8000

ENV SERVICE_NAME=${SERVICE_DIR}
ENV SERVICE_DIR=${SERVICE_DIR}
ENV PORT=${PORT}

EXPOSE ${PORT}

# Default entrypoint: uv run uvicorn ${SERVICE_NAME}.main:app --host 0.0.0.0 --port <PORT>
ENTRYPOINT ["sh", "-c", "exec uv run uvicorn ${SERVICE_NAME}.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS:-1} --loop uvloop --http httptools"]
