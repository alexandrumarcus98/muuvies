FROM node:26-alpine AS frontend-builder

WORKDIR /build/frontend
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN --mount=type=cache,id=pnpm,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile
COPY frontend/ ./
RUN pnpm build

FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=1

# System dependencies & health check tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Non-root user setup
RUN groupadd -g 1000 appuser && \
    useradd -u 1000 -g appuser -s /bin/bash -m appuser

# Dependencies
COPY backend/requirements.txt requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Application code & data mount point
COPY backend/ /app/backend/
COPY --from=frontend-builder /build/backend/static/app /app/backend/static/app
COPY pyproject.toml /app/pyproject.toml

RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://127.0.0.1:8888/api/health || exit 1

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8888", "--proxy-headers", "--forwarded-allow-ips=127.0.0.1,172.16.0.0/12", "--no-access-log"]

