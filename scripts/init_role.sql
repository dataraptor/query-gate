-- QueryGate Layer 3 — the least-privilege read-only DB role (spec §5).
--
-- This is the bedrock guarantee: the server connects as `querygate`, a role that
-- *physically lacks* write privileges. No INSERT/UPDATE/DELETE/CREATE grant exists,
-- so the boundary holds "even if every line of application code were wrong."
--
-- Run as the admin/migration role AFTER scripts/schema.sql has created app.* tables
-- (the GRANT below targets existing tables). Idempotent: safe to run repeatedly.
--
-- The role password comes from the `pw` psql variable, never hard-coded here:
--   psql -v pw="$QUERYGATE_DB_PASSWORD" -f init_role.sql
-- If `pw` is unset or empty it falls back to 'querygate_pw' (dev default only).

\if :{?pw}
\else
  \set pw ''
\endif
SELECT COALESCE(NULLIF(:'pw', ''), 'querygate_pw') AS pw \gset
-- Stash the resolved password in a session-local custom GUC so the DO block can read it.
SELECT set_config('querygate.pw', :'pw', false);

-- Create the login role only if absent (DROP would fail while it holds grants).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'querygate') THEN
        EXECUTE format('CREATE ROLE querygate LOGIN PASSWORD %L', current_setting('querygate.pw'));
    ELSE
        EXECUTE format('ALTER ROLE querygate LOGIN PASSWORD %L', current_setting('querygate.pw'));
    END IF;
END
$$;

-- Read-only by default at the role level (defense in depth; the load-bearing
-- guarantee is the *absence of write grants* below, which this complements).
ALTER ROLE querygate SET default_transaction_read_only = on;
-- Conservative memory ceiling (spec §5 memory note) so a heavy SELECT can't exhaust RAM.
ALTER ROLE querygate SET work_mem = '16MB';
-- Put the `app` schema on the role's search_path so an agent that only knows the bare table
-- names list_tables reports (e.g. "patients") can query them without guessing the schema.
-- Security-neutral: search_path grants nothing; the SELECT-only privileges below are unchanged.
ALTER ROLE querygate SET search_path = app, public;

-- Schema access: USAGE only on `app`; nothing writable.
GRANT USAGE ON SCHEMA app TO querygate;
-- SELECT only on every existing app table. No INSERT/UPDATE/DELETE/TRUNCATE, no sequence
-- USAGE (so even nextval/setval are unreachable — stricter than read-only alone).
GRANT SELECT ON ALL TABLES IN SCHEMA app TO querygate;
-- Future tables created by the admin in `app` are auto-granted SELECT (and nothing else).
ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO querygate;

-- Lock down `public`: the role must not be able to CREATE objects anywhere.
REVOKE CREATE ON SCHEMA public FROM querygate;
REVOKE ALL ON SCHEMA public FROM querygate;
