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

# Copy only what the production runtime actually needs, instead of
# blindly `COPY . .`-ing the whole repo (tests/, docs/, postman/,
# colab_training/, datasets/ml/ training data, etc. never need to be
# in the image - see .dockerignore for the full exclusion list and the
# reasoning). Each path below is preserved at the SAME relative
# location under WORKDIR /app that the code already expects:
#   - app/                     -> app code + app/ai_engine/saved_models/*.pkl
#                                  (crowd_model.pkl, delay_model.pkl,
#                                  frequency_model.pkl - loaded via
#                                  paths relative to app/ai_engine/prediction/)
#   - datasets/source/         -> the 4 real source CSV.gz files the
#                                  simulator (csv_replay_simulator.py),
#                                  seed_real_data.py, and the
#                                  crowd/delay/frequency metrics modules
#                                  read via ../../datasets/source-style
#                                  relative paths. datasets/ml/ (training-
#                                  only datasets) is intentionally NOT
#                                  copied - nothing under app/ reads it.
#   - migrations/ + alembic.ini -> needed for `alembic upgrade head`,
#                                  which both docker-compose.yml and
#                                  Render's Pre-Deploy Command run
#                                  against this same image before the
#                                  app starts serving traffic.
# requirements.txt is deliberately NOT copied here - dependencies are
# already installed into /usr/local from the builder stage above, so
# it's not read by anything at runtime.
COPY app/ ./app/
COPY datasets/source/ ./datasets/source/
COPY migrations/ ./migrations/
COPY alembic.ini ./alembic.ini

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
