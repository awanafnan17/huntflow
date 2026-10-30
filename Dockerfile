# ── Stage 1: Build Dependencies ─────────────────────────────────
FROM python:3.12-slim-bookworm AS deps

RUN apt-get update && apt-get install -y --no-install-recommends \
    git gcc libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

COPY ./packages /packages
COPY ./requirements /requirements
ARG BUILD_ENV=production
RUN uv pip install --system --no-cache -r /requirements/${BUILD_ENV}.txt

# ── Stage 2: Runtime ────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 postgresql-client curl nano procps \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from build stage
COPY --from=deps /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Install Playwright Chromium in shared location
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

RUN playwright install --with-deps chromium

# Create non-root huntflow user
RUN useradd -m -u 1000 -s /bin/bash huntflow

ARG APP_HOME=/app
WORKDIR ${APP_HOME}

# Copy start and entrypoint scripts
COPY ./compose/production/entrypoint /entrypoint
COPY ./compose/production/start-web /start-web
COPY ./compose/production/start-worker /start-worker
COPY ./compose/production/start-scheduler /start-scheduler
COPY ./compose/production/start-bootstrap /start-bootstrap
RUN chmod +x /entrypoint /start-web /start-worker /start-scheduler /start-bootstrap

# Copy application source
COPY --chown=huntflow:huntflow . ${APP_HOME}

# Ensure directory permissions
RUN mkdir -p /app/staticfiles /app/media /app/private_media /app/data /app/artifacts \
    && chown -R huntflow:huntflow /app

ENTRYPOINT ["/entrypoint"]
CMD ["/start-web"]
