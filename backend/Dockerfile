# ==============================================================================
# Disaster Safety API - Production Container Image
# Multi-stage / lightweight security-hardened container for FastAPI
# ==============================================================================

FROM python:3.13-slim as base

# System security settings
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install runtime OS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application user
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/bash -m appuser

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# Copy application source code and migrations
COPY alembic.ini .
COPY alembic/ ./alembic/
COPY app/ ./app/
COPY gunicorn_conf.py .

# Change ownership to non-root user
RUN chown -R appuser:appgroup /app

# Switch to non-privileged user
USER appuser

EXPOSE 8000

# Container liveness health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Production ASGI server entrypoint
CMD ["gunicorn", "-c", "gunicorn_conf.py", "app.main:app"]
