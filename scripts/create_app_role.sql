-- Create the unprivileged role the application should connect as.
--
-- Run this against the database as an owning or superuser role, *after* migrations. The password
-- arrives as a session setting rather than a psql variable, so that the same file can be executed by
-- psql and by a plain database driver — `scripts/provision_app_role.py` runs it on every deploy, and
-- a second hand-maintained copy of these grants would drift from this one within a release:
--
--   psql "$ADMIN_URL" -v ON_ERROR_STOP=1 \
--     -c "SET proofstep.role_password = 'PW'" \
--     -f scripts/create_app_role.sql
--
-- Re-running is safe and expected: it rotates the password and re-applies the grants, which is how
-- tables added by the latest migration become reachable by the application role.
--
-- Then point the application at it:
--
--   POSTGRES_USER=proofstep_app
--   POSTGRES_PASSWORD=<the password you generated>
--
-- ## Why this is not optional
--
-- Postgres exempts superusers and roles with BYPASSRLS from every row-level-security policy,
-- unconditionally — FORCE ROW LEVEL SECURITY does not reach them. An application that connects as
-- the database superuser has RLS installed, enabled, forced, policied, and completely inert, with
-- nothing in its behaviour to suggest it. `GET /readyz` reports this, and `proofstep doctor` will
-- tell you which role you are using.
--
-- Migrations still run as an owner or superuser: they create tables, and the application role
-- deliberately cannot.

-- The password is read from `proofstep.role_password`, set by the caller. Passing it through a
-- setting keeps it out of this file, out of the shell's argument list, and — because the role name
-- is fixed and the value goes through `quote_literal` — out of reach of SQL injection.
--
-- `quote_literal` rather than the idiomatic `format()` with a literal specifier: this file is also
-- executed by psycopg, which scans the whole statement -- comments included -- for its own
-- client-side placeholders and refuses anything starting with a percent sign that it does not
-- recognise. The two are equivalent here; only one survives both callers. Which is also why no
-- percent sign appears anywhere in this file.
DO $$
DECLARE
  pw text := current_setting('proofstep.role_password', true);
BEGIN
  IF pw IS NULL OR pw = '' THEN
    RAISE EXCEPTION
      'proofstep.role_password is not set. Run: SET proofstep.role_password = ''''<password>''';
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'proofstep_app') THEN
    EXECUTE 'CREATE ROLE proofstep_app LOGIN PASSWORD ' || quote_literal(pw);
  END IF;

  -- Idempotent: re-running rotates the password rather than failing.
  EXECUTE 'ALTER ROLE proofstep_app PASSWORD ' || quote_literal(pw);
END
$$;

-- Unconditional and deliberate: if someone granted this role SUPERUSER or BYPASSRLS to work around
-- a permissions problem, every policy in the database is inert until it is taken back.
ALTER ROLE proofstep_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;

-- Exactly what the application needs: read and write the data, and nothing structural. It cannot
-- create or drop tables, which is also why it cannot own them — and a non-owner is subject to RLS
-- even without FORCE.
GRANT USAGE ON SCHEMA public TO proofstep_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO proofstep_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO proofstep_app;

-- Future tables too. Without this, the next migration's tables are unreadable by the application
-- and the deployment breaks on deploy rather than on install — the more confusing of the two.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO proofstep_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO proofstep_app;

-- Explicitly *not* granted: CREATE on the schema, TRUNCATE, REFERENCES, and any ownership. The
-- application has no reason to reshape the schema, and a role that cannot is a role that cannot be
-- used to disable a policy.

SELECT
  rolname AS role,
  rolsuper AS is_superuser,
  rolbypassrls AS bypasses_rls,
  CASE WHEN rolsuper OR rolbypassrls THEN 'RLS WOULD NOT APPLY' ELSE 'rls applies' END AS status
FROM pg_roles
WHERE rolname = 'proofstep_app';
