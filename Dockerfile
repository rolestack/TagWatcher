# =============================================================================
# Stage 1: Builder
# =============================================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install into a virtual environment
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:3.12-slim AS runtime

# Install only runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Use the venv by default
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Create a non-root user
RUN groupadd --gid 1001 tagwatcher && \
    useradd --uid 1001 --gid tagwatcher --shell /bin/bash --create-home tagwatcher

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=tagwatcher:tagwatcher . .

# Create directories that the app may need to write to
# /app/data must be mounted as a Docker volume so tagwatcher.json persists
RUN mkdir -p /app/data /app/logs && chown -R tagwatcher:tagwatcher /app/data /app/logs

# Switch to non-root user
USER tagwatcher

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

EXPOSE 8000

# Run alembic only when a DB URL is already available (skipped on first boot).
# On first boot the setup wizard initialises the schema via SQLAlchemy create_all.
CMD ["sh", "-c", "\
  DB_URL=$(python -c \"from app.config_file import get_database_url; u=get_database_url(); print(u or '')\" 2>/dev/null); \
  if [ -n \"$DB_URL\" ]; then \
    echo 'Running database migrations...' && alembic upgrade head || exit 1; \
  else \
    echo 'No database configured yet — skipping migrations (setup wizard will initialise the schema).'; \
  fi && \
  exec gunicorn app.main:app \
    --workers ${WORKERS:-2} \
    --worker-class ${WORKER_CLASS:-uvicorn.workers.UvicornWorker} \
    --bind ${BIND:-0.0.0.0:8000} \
    --timeout ${TIMEOUT:-120} \
    --keep-alive ${KEEPALIVE:-5} \
    --access-logfile - \
    --error-logfile - \
    --log-level ${LOG_LEVEL:-info}"]
