-- Create the unprivileged role the application should connect as.
--
-- Run this once per database, as a superuser, *after* migrations:
--
--   psql "$ADMIN_URL" -v role_password="$(openssl rand -hex 24)" -f scripts/create_app_role.sql
--
-- Then point the application at it:
--
--   POSTGRES_USER=evalforge_app
--   POSTGRES_PASSWORD=<the password you generated>
--
-- ## Why this is not optional
--
-- Postgres exempts superusers and roles with BYPASSRLS from every row-level-security policy,
-- unconditionally — FORCE ROW LEVEL SECURITY does not reach them. An application that connects as
-- the database superuser has RLS installed, enabled, forced, policied, and completely inert, with
-- nothing in its behaviour to suggest it. `GET /readyz` reports this, and `evalforge doctor` will
-- tell you which role you are using.
--
-- Migrations still run as an owner or superuser: they create tables, and the application role
-- deliberately cannot.

\set ON_ERROR_STOP on

-- Created with \gexec rather than a DO block, because psql does not substitute :variables inside
-- dollar-quoted strings — so the password would arrive literally as ":'role_password'".
SELECT format(
  'CREATE ROLE evalforge_app LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE',
  :'role_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evalforge_app')
\gexec

-- Idempotent: re-running rotates the password rather than failing.
ALTER ROLE evalforge_app PASSWORD :'role_password';
ALTER ROLE evalforge_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- Exactly what the application needs: read and write the data, and nothing structural. It cannot
-- create or drop tables, which is also why it cannot own them — and a non-owner is subject to RLS
-- even without FORCE.
GRANT USAGE ON SCHEMA public TO evalforge_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO evalforge_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO evalforge_app;

-- Future tables too. Without this, the next migration's tables are unreadable by the application
-- and the deployment breaks on deploy rather than on install — the more confusing of the two.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO evalforge_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO evalforge_app;

-- Explicitly *not* granted: CREATE on the schema, TRUNCATE, REFERENCES, and any ownership. The
-- application has no reason to reshape the schema, and a role that cannot is a role that cannot be
-- used to disable a policy.

SELECT
  rolname AS role,
  rolsuper AS is_superuser,
  rolbypassrls AS bypasses_rls,
  CASE WHEN rolsuper OR rolbypassrls THEN 'RLS WOULD NOT APPLY' ELSE 'rls applies' END AS status
FROM pg_roles
WHERE rolname = 'evalforge_app';
