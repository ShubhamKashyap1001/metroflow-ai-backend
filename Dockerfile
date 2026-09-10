# ---- MetroFlow AI Backend ----
# Multi-stage build: keep the final runtime image small by installing
# Python deps into a venv-like prefix in the build stage, then copying
# only the installed packages + app code into a slim runtime image.

FROM python:3.12-slim AS builder

WORKDIR /build

# System deps needed to build wheels for psycopg2-binary / xgboost / scipy.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

WORKDIR /app

# libgomp1 is required at runtime by xgboost/scikit-learn (OpenMP),
# libpq5 by psycopg2-binary's dynamic linking.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

# Copy the whole backend repo, preserving the app/ + datasets/ layout
# the code expects at runtime (see app/simulator/csv_replay_simulator.py
# and app/ai_engine/saved_models/*.pkl, both referenced via relative
# paths from inside app/).
COPY . .

# Run as a non-root user.
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

# Render/ECS/EC2 all set $PORT (or you can hardcode 8000) - default to
# 8000 for docker-compose / plain `docker run` use.
ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY:-1}"]
