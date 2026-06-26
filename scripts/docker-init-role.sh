#!/usr/bin/env bash
# docker-entrypoint-initdb.d hook (runs once, on first cluster init, AFTER 01-schema.sql).
# Applies the Layer-3 role/grants, passing the read-only role password from the environment
# (QUERYGATE_DB_PASSWORD) into init_role.sql's `pw` variable — so it is never hard-coded.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     -v pw="${QUERYGATE_DB_PASSWORD:-querygate_pw}" \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -f /init/init_role.sql
