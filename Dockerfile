# QueryGate application image — the seeder + the read-only MCP server / web demo.
#
# One image, used by two compose services:
#   * `seeder` runs `querygate seed --reset` once (as the admin role) to load the fixed seed.
#   * `web`    runs `querygate web` (the static app/ UI + the live /api/* agent loop) on :8000.
#
# The Postgres + Layer-3 role/grants come from the `postgres:16` service in docker-compose.yml
# (initdb hooks apply scripts/schema.sql then scripts/init_role.sql) — not from this image.
FROM python:3.12-slim

# psycopg[binary] ships its own libpq, so no system postgres client is needed for the app. A tiny
# curl is added only for the compose healthcheck; keep the layer small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the whole repo (the .dockerignore trims tests/, .git, pgdata, .env, caches). The runtime needs
# the package code PLUS its sibling dirs at the repo root: REPO_ROOT is derived from the package file
# location (querygate/cli.py -> /app), and `querygate seed`/`web` read /app/scripts, /app/evals, /app/app.
COPY . .

# Editable install so the package resolves to /app/querygate (REPO_ROOT == /app) and the sibling
# scripts/ evals/ app/ dirs are found at runtime — a non-editable install would land in site-packages
# and break those paths. `.[eval]` adds openai + python-dotenv so the live /api/ask can answer with a
# model key. PYTHONPATH=/app makes `from evals import run_eval` resolve from the console-script entry.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[eval]"
ENV PYTHONPATH=/app

# A writable place for the per-process audit log (overridable via QUERYGATE_AUDIT_PATH).
RUN mkdir -p /data
ENV QUERYGATE_AUDIT_PATH=/data/audit.jsonl

EXPOSE 8000

# Default to the web demo, bound to 0.0.0.0 so Docker port mapping can reach it (compose publishes the
# host port on 127.0.0.1 to keep the loopback-only posture). Compose overrides `command` for the seeder.
CMD ["querygate", "web", "--host", "0.0.0.0", "--port", "8000"]
